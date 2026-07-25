# Disconnect Watcher — Analysis

> How `aiofence.contrib.starlette` interacts with the rest of the ASGI stack, and where it
> breaks. Supersedes and absorbs the earlier `receive-channel-conflicts.md`.

**Environment:** Python 3.12 · Starlette 0.52.1 · FastAPI 0.140.0 · anyio 4.12.1 ·
sse-starlette 3.4.6. Server behaviour cross-checked against uvicorn 0.51.0, hypercorn
0.18.0, daphne 4.2.3, granian 2.7.9, and the ASGI HTTP spec.

Findings carry a **verification** tag:

- **reproduced** — reproduced directly against the installed stack
- **agent-reproduced** — reproduced by a diagnostic pass, premises independently source-checked
- **source-derived** — established by reading framework source; not executed

---

## The two rules

Everything below follows from two properties of the ASGI receive channel.

### Rule 1 — the channel has exactly one useful reader

`receive()` is a queue pop, not a broadcast: whichever caller is parked first gets the next
message, and everyone else gets the ones after it. There is no way to peek, and no way to
put a message back.

The disconnect watcher in `contrib/starlette.py` is a reader. It loops on `receive()` and
discards everything that isn't `http.disconnect`. So every conflict in [Part A](#part-a) is
the same conflict — something else in the stack also wants to read the channel, and the two
split the message stream between them.

This is why `_shared_disconnect_event` caches its event in the ASGI scope. `disconnect_event`
and `disconnect_fencing` on one endpoint used to start two watchers, and neither was
guaranteed to see the disconnect. **Inside our own module, one reader is something we can
enforce. Outside it, it isn't.**

### Rule 2 — `http.disconnect` does not mean "the client disconnected"

Per the ASGI HTTP spec, the message is

> "Sent to the application if receive is called after a response has been sent **or** after
> the HTTP connection has been closed."

It is a *stream-ended* signal, not a *client-gone* signal. The watcher's predicate
(`starlette.py:121`) reads it as the latter. This is [D1](#d1), and it fires on every
successful request.

The earlier doc already recorded this trap — in the "what middleware must get right"
section: *"uvicorn emits `http.disconnect` as soon as the response completes, so naive
middleware cancels background tasks on every normal request."* The finding here is that the
**current dependency implementation has exactly that bug today**, for the same reason, and
FastAPI's teardown ordering guarantees it rather than making it a race.

---

## Verdict

The sharing fix in `749965d` is correct on its own terms: the scope cache does yield one
watcher per request, and FastAPI's request-scoped `AsyncExitStack` unwinds LIFO, so the
watcher's owner is always the last to tear down. Dependency caching, scope identity, solve
order, ContextVar propagation and the cancel-counter protocol were all checked and are sound
— see [checked and clean](#clean).

The defects are elsewhere. One is blocking and fires on every successful request; two are
silent correctness bugs in the shared-watcher design itself, one of them a regression
introduced by `749965d`.

| # | Finding | Severity | Verification |
|---|---|---|---|
| [D1](#d1) | `http.disconnect` also means "response finished" — false disconnect on every successful request; kills `BackgroundTasks` | **blocking** | reproduced |
| [D2](#d2) | Two disconnect codes on one request collapse — first code silently destroyed | **high** | reproduced |
| [D3](#d3) | Scope cache outlives its watcher — later callers get a dead `Event` | **high** | reproduced |
| [D4](#d4) | Watcher exceptions are invisible, then land after the response was sent | **high** | agent-reproduced |
| [D5](#d5) | Body theft: watcher eats `http.request`; unparsed reads hang or return `b""` | **high** | reproduced |
| [D6](#d6) | `StreamingResponse` rival reader — the "2.4 is safe" escape hatch almost never applies | **high** | reproduced |
| [D7](#d7) | sse-starlette: we win deterministically and silently disable its close handler | **high** | reproduced |
| [D8](#d8) | Non-uvicorn servers deliver `http.disconnect` exactly once — starvation | **high** | source-derived |
| [D9](#d9) | `BaseHTTPMiddleware` `wrapped_receive` is non-reentrant; also masks D1 | medium | source-derived |
| [D10](#d10) | No version floor — FastAPI 0.106–0.117 tears the watcher down before the response | medium | reproduced (pin absent) |
| [D11](#d11) | `Depends(..., scope="function")` inverts the LIFO invariant the design relies on | medium | agent-reproduced |
| [D12](#d12) | Sync `def` handlers — documented as working; raises `RuntimeError` | medium | reproduced |
| [D13](#d13) | Exception handlers see no fencing; teardown order inverts on the error path | medium | agent-reproduced |
| [D14](#d14) | asyncio-only primitives, no Trio guard | low | source-derived |
| [D15](#d15) | `asyncio.shield` in teardown is inert, and hides one error path | low | agent-reproduced |
| [D16](#d16) | Version-bounded / server-specific hazards | low | source-derived |

---

<a id="d1"></a>
## D1 — The signal is overloaded — **blocking**

**Trigger:** every normally-completing request. No client disconnect required.

Every server collapses "response finished" and "client gone" into one message: uvicorn
(`send()` sets `response_complete = True; message_event.set()`, then `receive()` returns the
disconnect), hypercorn `http_stream.py:243-247`, daphne `http_protocol.py:263-274`, granian
`src/asgi/io.rs:127-138`. Starlette's own `TestClient` does the same
(`testclient.py:292-298`).

FastAPI keeps the watcher alive across that moment. Yield dependencies live on
`request_astack`, and `fastapi/routing.py:138-145` puts `await response(...)` *inside* it:

```python
async with AsyncExitStack() as request_stack:      # yield-dependency teardown
    scope["fastapi_inner_astack"] = request_stack
    async with AsyncExitStack() as function_stack:
        response = await f(request)
    await response(scope, receive, send)           # response_complete flips HERE
    response_awaited = True
# request_stack.__aexit__ -> listener.cancel() runs LAST
```

This is the same ordering recorded earlier as *"teardown ordering is in our favour"* — and
it is, for keeping the `Fencing` alive across a stream ([D6](#d6)). The cost is that the
watcher is **guaranteed** to be parked in `receive()` when the response completes, wakes with
`http.disconnect`, and calls `event.set()`.

### Consequence: `BackgroundTasks` are cancelled on every successful request

`Response.__call__` runs `await self.background()` *after* the final body send
(`starlette/responses.py:164-167`) — inside the window between response completion and
watcher teardown. Reproduced with a client that never disconnects:

```python
app = FastAPI(dependencies=[Depends(disconnect_fencing)])   # the app-wide pattern docs/api.md:343 recommends

async def work() -> None:
    with get_current_fencing().move_on_cancel() as fence:
        await asyncio.sleep(0.05)

@app.get("/work")
async def handler(bt: BackgroundTasks) -> dict[str, bool]:
    bt.add_task(work)
    return {"ok": True}
```
```
response: {'ok': True}
receive() returned: ['http.disconnect']
background task: {'bg_completed': False, 'bg_cancelled_by_disconnect': True}
```

The task is cancelled and reports `cancelled_by("disconnect") == True` on a request where the
client never left. Because `EventTrigger.check()` (`triggers/event.py:26-29`) pre-triggers on
an already-set event, *any* `Fence` opened after this point is cancelled instantly.

**Not implementation-specific.** [fastapi#11360](https://github.com/fastapi/fastapi/discussions/11360)
is the community's canonical drain-and-replay middleware, and its known reported defect is
exactly this. Any fix must distinguish "client left" from "response finished", and the
wire-level `receive()` cannot — both collapse into one message via sticky
`disconnected` / `response_complete` flags. This is why [the fix](#the-fix) has to track
`response_complete` in a wrapped `send`.

**Masked by `BaseHTTPMiddleware`** ([D9](#d9)), so it looks intermittent across deployments.

*A secondary consequence claimed by one diagnostic pass — that dependencies yielding after
`disconnect_event` observe `gone.is_set() == True` in their own teardown — did **not**
reproduce here; teardown won the race. Timing-dependent, not deterministic.*

---

<a id="part-a"></a>
# Part A — Channel conflicts (Rule 1)

<a id="d5"></a>
## D5 — Raw body reads — **high**

Reading the raw body while a fencing is bound races the watcher:

```python
@app.post("/upload", dependencies=[Depends(disconnect_fencing)])
async def handler(request: Request):
    await something()             # watcher parks in receive() here
    body = await request.body()   # may hang forever
```

**It is a race, not a deterministic hang.** Probing four cases:

| case | outcome |
|---|---|
| raw read immediately, no suspension | works — by luck |
| raw read after any `await` | **hangs** |
| FastAPI-parsed body param | safe |
| parsed param + suspension + explicit `request.body()` | safe |

The two safe rows have the same cause: FastAPI reads and caches the body at
`routing.py:428/431`, *before* `solve_dependencies` at `:479`. Those messages are consumed
before the watcher exists, and a later `request.body()` returns cached bytes without touching
`receive()`. `get_flat_dependant` aggregates body params from sub-dependencies too, so
`Body`/pydantic models and `UploadFile`/multipart are all covered.

The practical trap is that the first row passes in tests and hangs in production — under a
real server the watcher is normally parked well before the body arrives over the network.

### Two things the earlier write-up understated

**The unsafe shape is wider than "raw reads".** `fastapi/routing.py:426` gates the pre-read
on `if body_field:`, which is `None` when **no body param is declared**. A handler taking
only `Request` is therefore unprotected by construction — not merely when it chooses to read
raw.

**The failure is not always a hang — it can be silent truncation.** A stolen chunk is
unrecoverable: `Request.stream()` never sets `_stream_consumed` (`requests.py:240`) or
`_body` (`requests.py:253`). And because uvicorn does not reset `more_body`, the losing
reader can receive `{"type": "http.request", "body": b"", "more_body": False}`, which
Starlette accepts as a **complete, empty body**:

```
POST, headers then body 300ms later
  WITHOUT watcher -> request.body() = b'{"query":"IMPORTANT PAYLOAD"}'
  WITH watcher    -> request.body() = b''
```

No exception, no timeout, no log. One further undocumented path:
`fastapi/routing.py:2160-2171` (`_FrontendRouteGroup.handle`) solves dependencies *before*
`route.handle`, defeating the pre-read guarantee.

---

<a id="d6"></a>
## D6 — Starlette `StreamingResponse` — **high**

`StreamingResponse.__call__` starts its own `listen_for_disconnect` on the same channel, but
only on one branch (`starlette/responses.py:261-263`):

```python
spec_version = tuple(map(int, scope.get("asgi", {}).get("spec_version", "2.0").split(".")))
if spec_version >= (2, 4):
    await self.stream_response(send)      # no second reader
else:
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(wrap, partial(self.stream_response, send))
        await wrap(partial(self.listen_for_disconnect, receive))
```

Below `2.4` there are two readers, and the winner decides the outcome:

| winner | result |
|---|---|
| our watcher | fence fires, body resumes after `move_on_cancel()`, stream completes; Starlette's listener parks forever |
| Starlette | disconnect event never set, `cancelled_by("disconnect")` stays `False`, stream aborted mid-body by anyio's cancel scope |

**Within that branch, it turns on whether the handler suspended before returning the
response.** With no `await` in the handler, the watcher task hasn't been scheduled yet when
`StreamingResponse.__call__` calls `receive()`, so Starlette wins. Adding a single
`await asyncio.sleep(0)` flips it. Reproduced in both directions.

### The escape hatch almost never applies

`docs/api.md:397` says servers declaring 2.4+ "skip Starlette's listener entirely, and the
fencing behaves normally there". True as written, but it implies the race is an edge case.
It is the norm:

| Server / version | HTTP `spec_version` | Rival reader? |
|---|---|---|
| uvicorn ≤ 0.27.1 | `2.1` → `2.3` | yes |
| uvicorn 0.28.0 – 0.32.0 | `2.4` | no |
| **uvicorn 0.32.1 – 0.51.0 (current)** | **`2.3`** | **yes** |
| hypercorn 0.14 – 0.18 | `2.1` | yes |
| daphne 4.2.3 | *absent* → defaults `2.0` | yes |
| granian 2.7.9 | `2.3` | yes |
| Starlette `TestClient` | *no `asgi` key* → `2.0` | yes |

uvicorn's brief move to 2.4 was reverted in 0.32.1 ("Drop ASGI spec version to 2.3 on HTTP
scope", #2513). **Under every current production server and under the test client, the
two-reader race is the only path.** The gate itself only exists in Starlette ≥ 0.42.0;
0.36–0.41.x run `listen_for_disconnect` unconditionally on every server.

When Starlette wins it cancels the body via its task group and *swallows* the cancellation
(`responses.py:274`), then still runs `self.background()`.

`Response`, `FileResponse` and `middleware/base._StreamingResponse` were checked and read
`receive` nowhere — the conflict is specific to `StreamingResponse` and SSE.

---

<a id="d7"></a>
## D7 — sse-starlette `EventSourceResponse` — **high**

The same conflict, deterministic, with the outcome reversed.

`EventSourceResponse` reads the channel **unconditionally** — there is no `2.4` escape hatch.
This is deliberate, per its module docstring ("Divergence #1"): the loop also drives
`client_close_handler_callable` and clears `self.active`, which is what stops `_ping` and the
shutdown grace loop.

Its listener is started **last** (`# Wait for the client to disconnect last`), so our watcher
— parked since dependency-solving time — wins consistently. Verified across all four
combinations of handler-suspends × `spec_version`.

Measured against an unfenced control:

| | `client_close_handler` | pings after disconnect | generator |
|---|---|---|---|
| unfenced | fires | 1, then stops | aborted |
| fenced | **never fires** | 6, still going | runs to completion |

So fencing an SSE endpoint gets you working cancellation while silently switching off
sse-starlette's own disconnect handling. For an LLM token-streaming endpoint that is a bad
shape: cancellation appears to work, but a close handler doing cleanup, billing, or metrics
never runs, and pings keep going to a closed socket.

Covered by `tests/contrib/test_sse_starlette.py`, which asserts both halves.

### Aside: the shutdown watcher

sse-starlette starts one `_shutdown_watcher` task per *thread* (`threading.local`, not per
event loop) on first use, and it outlives every request by design. Not a leak, but it trips
strict per-test asyncio accounting — the test module cancels it per test rather than
loosening the suite invariant. Note the `watcher_started` flag is only cleared in the
watcher's `finally`, so a thread that reuses a fresh event loop can end up with the flag set
and no live watcher.

---

<a id="d8"></a>
## D8 — Non-uvicorn servers deliver `http.disconnect` exactly once — **high**

uvicorn recomputes a predicate on every call, so *every* reader sees the disconnect. That is
what makes [D6](#d6) survivable there and what keeps `Request.is_disconnected()` working. The
other three enqueue it once:

- **hypercorn 0.18.0** — `receive` is `asyncio.Queue.get`; `http_stream.py:81-132` guards
  `if self.closed: return`. A second `receive()` **parks forever**.
- **daphne 4.2.3** — one `put_nowait` in `send_disconnect()`; a second `receive()` blocks
  until the 10 s `application_close_timeout` reaper fires.
- **granian 2.7.9** — repeats only once `flow_rx_closed` latches; the
  client-aborted-mid-upload path returns disconnect *without* latching, so the next call
  hangs.

Consequence: whichever of {watcher, `listen_for_disconnect`, `is_disconnected()`} calls
`receive()` first consumes the only disconnect; the others never fire. On these servers the
watcher **silently disables** `StreamingResponse`'s abort-on-disconnect, or vice versa — the
[D6](#d6) table stops being a race and becomes a permanent loss for whoever came second.

This also settles `Request.is_disconnected()`: it keeps working on uvicorn via re-delivery,
and breaks on the other three. It is independently broken under `BaseHTTPMiddleware` on all
servers ([starlette#2094](https://github.com/Kludex/starlette/discussions/2094), ≥ 0.21.0),
so it is not a viable fallback either way.

---

<a id="d9"></a>
## D9 — `BaseHTTPMiddleware` — medium

With any `BaseHTTPMiddleware` in the stack, the watcher is handed `receive_or_disconnect`
(`middleware/base.py:144`), wrapping `_CachedRequest.wrapped_receive` (`base.py:34-93`) — a
non-reentrant state machine over a single shared async generator.

- **Reentrancy.** The watcher holds it continuously. Middleware touching the body after
  `call_next` (e.g. logging) produces a second concurrent `__anext__` →
  `RuntimeError: anext(): asynchronous generator is already running`, or corrupted cache
  state. Not reachable through Starlette's own code — `_StreamingResponse` stopped calling
  `receive()` in [starlette#2620](https://github.com/encode/starlette/pull/2620) — so this
  needs user middleware.
- **It raises into the watcher.** `base.py:57` raises
  `RuntimeError(f"Unexpected message received: ...")` directly into the detached task,
  feeding [D4](#d4). This is the most likely real-world trigger for it.
- **It masks [D1](#d1).** `receive_or_disconnect` only unblocks at `response_sent.set()`
  (`base.py:195`), which lands after downstream teardown already cancelled the watcher — so
  the spurious disconnect disappears under middleware and reappears without it.
- Real disconnects still propagate correctly through it.

`WSGIMiddleware` cannot host the dependency at all; `ServerErrorMiddleware` /
`ExceptionMiddleware` never touch `receive`. These state machines are unchanged from
Starlette 0.36 through 1.3.1.

---

# Part B — The sharing design (`749965d`)

<a id="d2"></a>
## D2 — Two disconnect codes on one request collapse — **high, regression**

**Trigger:** `disconnect_fencing` (or `disconnect_fencing_dependency(code="a")`) and a second
variant with a different code, active on the same request.

`Fencing.event()` deduplicates by **event identity, last code wins** (`api.py:120-122`):

```python
existing = tuple(e for e in self._events if e.event is not event)
return self._derive(_events=(new_entry, *existing))
```

Before `749965d` each dependency minted its own `asyncio.Event`, so two codes produced two
entries. Now the shared watcher hands out the same object, so the earlier registration is
silently replaced. Reproduced with the exact layering `docs/api.md:340-348` recommends:

```python
app = FastAPI(dependencies=[Depends(disconnect_fencing)])                       # code="disconnect"
router = APIRouter(dependencies=[Depends(disconnect_fencing_dependency(code="client_gone"))])
```
```
codes in ambient fencing    -> ['client_gone']      # "disconnect" is gone
cancelled_by("disconnect")  -> False
cancelled_by("client_gone") -> True
```

Shared/library code doing `fence.cancelled_by("disconnect")` silently stops working the
moment any router overrides the code. This is the direct cost of unifying the event object —
the sharing fix solved the two-readers problem and created a two-codes problem.

---

<a id="d3"></a>
## D3 — Scope cache outlives the watcher it points at — **high**

`request.scope[_SCOPE_KEY] = event` (`starlette.py:108`) is never removed, while the listener
is owned solely by the first entrant and cancelled in *its* `finally` (`starlette.py:114-115`).
Any later entrant takes the `existing is not None` fast path (`starlette.py:102-105`) and
receives an `Event` whose watcher is gone — no liveness check, and a `finally` that is now a
no-op.

Reproduced two ways:

**Sequential reuse** (the plain-Starlette path the module advertises):
```
same event reused after teardown: True
total receive() calls:            0
event fired on second use:        False
scope key still present:          True
```

**Two concurrent entrants inside one handler** — the short-lived one owns the watcher and
tears it down under the long-lived one:
```python
async with asyncio.TaskGroup() as tg:
    tg.create_task(short())   # enters first, exits first -> kills the watcher
    tg.create_task(long())
```
```
{'long_sees_disconnect': False}       # disconnect arrived while long() was still running
```

Both failure modes are silent: a stale-unset event that can never fire, or (via [D1](#d1)) a
stale-set event that cancels everything instantly.

The happy path survives only because FastAPI's exit stack unwinds LIFO so the owner exits
last — an accident of FastAPI's ordering, not an invariant this module enforces. [D11](#d11)
is the supported FastAPI feature that breaks it.

---

<a id="d4"></a>
## D4 — Watcher exceptions: invisible, then late — **high**

`_quiet_await` (`starlette.py:126-128`) suppresses **only** `CancelledError`. If
`request.receive()` raises anything else, three things follow:

1. **During the request — completely silent.** The detached task (`starlette.py:109`) dies,
   the event can never fire, `fence.cancelled` stays `False`. No warning, no log. The feature
   stops working with zero signal.
2. **At teardown — propagates after the response was sent.** `starlette.py:115` re-raises out
   of `AsyncExitStack.__aexit__`, and per [D1](#d1) `await response(...)` already ran.
   Starlette cannot then emit a clean 500 (`errors.py:180`) and instead produces a truncated
   response plus `RuntimeError("Caught handled exception, but response already started.")`
   (`_exception_handler.py:55-56`).
3. **It can replace the handler's own exception.** With a handler raising
   `ValueError("ORIGINAL")`, the app was observed raising the watcher's `RuntimeError`
   instead — logging and error tracking attribute the wrong cause.

Realistic sources: `RuntimeError("Unexpected message received: ...")` from
`middleware/base.py:57` ([D9](#d9)); `RuntimeError("Receive channel has not been made
available")` from `requests.py:200-201`; any transport error; non-uvicorn servers.

---

# Part C — Environment and compatibility

<a id="d10"></a>
## D10 — No version floor for a behaviour the design depends on — medium

`pyproject.toml:16` declares `dependencies = []`, with no `fastapi`/`starlette` extra. The
only constraint anywhere is the dev group's `fastapi>=0.100` / `starlette>=0.27`.

Yield-dependency teardown timing has moved twice:

| FastAPI | Teardown runs | Effect on the watcher |
|---|---|---|
| ≤ 0.105 | after the response | fine |
| **0.106.0 – 0.117.x** | **before the response** | **watcher dead for the whole streaming phase** |
| ≥ 0.118.0 | after the response (reverted, PR #14099) | fine — and guarantees [D1](#d1) |
| ≥ 0.121.0 | adds `Depends(..., scope=...)` | see [D11](#d11) |

The *"teardown ordering is in our favour"* property is therefore a property of FastAPI
≥ 0.118 specifically, not of FastAPI. Nothing stops a consumer installing 0.106–0.117.
Starlette matters too: the [D6](#d6) gate needs ≥ 0.42.0.

---

<a id="d11"></a>
## D11 — `Depends(..., scope="function")` inverts the LIFO invariant — medium

FastAPI ≥ 0.121 lets a dependency opt into the function-scoped stack
(`dependencies/utils.py:705-708`), which `fastapi/routing.py:141` closes **before** the
response while `:145` closes the request stack after. Mixing
`Depends(disconnect_event, scope="function")` with a request-scoped `disconnect_fencing` is
legal — the `DependencyScopeError` guard (`utils.py:344-357`) only covers parent/child, not
siblings.

If the function-scoped one enters first it owns the watcher and is torn down first, inverting
the ordering that makes [D3](#d3) harmless:

```
HANDLER:    watchers alive=1
response sent
BACKGROUND: watchers alive=0  ambient=['disconnect']  event_is_the_same=True
streaming:  chunk0/1/2 -> watchers alive=0
```

The fencing stays bound and still references the orphaned event, so it can never fire.

---

<a id="d12"></a>
## D12 — Sync `def` handlers — medium

FastAPI runs `def` handlers in a threadpool. The contextvar is copied into the thread, so
`get_current_fencing()` returns the right `Fencing` — but entering a fence fails:

```
{'codes': ['disconnect'],
 'move_on_cancel': 'RuntimeError: no running event loop',
 'timeout':        'RuntimeError: no running event loop'}
```

`Fence.__enter__` calls `asyncio.current_task()` (`core.py:117`), and `EventTrigger.arm` /
`Fencing.timeout` call `get_running_loop()`.

It fails loudly at first use rather than silently never cancelling, so this needs
documentation rather than a guard — but `docs/api.md:350` currently promises the opposite:
*"the dependency still binds and `fence.cancelled_by(...)` still reports correctly"*.
Reaching `cancelled_by` raises, so following the docs verbatim produces a 500.

Related: `core.py:118` is a bare `assert task is not None`, which under `python -O` degrades
into a confusing `AttributeError` instead of a clean error.

No scheduling problem, though — sync handlers run in a thread and never block the loop, so
the watcher is scheduled normally.

---

<a id="d13"></a>
## D13 — Exception handlers see no fencing — medium

`fastapi/routing.py:156` applies `wrap_app_handling_exceptions` **outside** the exit stacks at
`:138-141`, so on the error path the stack unwinds *first*:

```
HANDLER(httpexc) ambient=['disconnect']
EXC HANDLER      ambient=None  watchers=0
SEND http.response.start
```

A custom handler cannot ask "was this a disconnect?", and teardown ordering relative to the
response **inverts** versus the happy path ([D1](#d1)) — worth documenting, since users will
reason from the success case.

---

<a id="d14"></a>
## D14 — asyncio-only primitives, no backend guard — low

Starlette 0.52.1 and FastAPI 0.140.0 contain zero `import asyncio`; both are pure anyio, and
`TestClient` explicitly supports `backend="trio"`. The module uses `asyncio.Event()`
(`starlette.py:107`), `asyncio.create_task` (`:109`) and `asyncio.shield` (`:115`) directly,
so under Trio `:109` raises `RuntimeError: no running event loop`.

Fails loudly rather than corrupting, and the constraint is inherited — `Fencing.event()` is
typed `asyncio.Event` (`api.py:110`), so aiofence is asyncio-only by design. Worth an
explicit statement rather than a guard.

---

<a id="d15"></a>
## D15 — `asyncio.shield` in the teardown is inert — low

`await asyncio.shield(_quiet_await(listener))` (`starlette.py:115`) was A/B tested against the
unshielded form across four scenarios — enclosing task already cancelled, `_must_cancel`
pending, a second cancel landing on the teardown await, and an already-completed listener.
The listener was reaped with **zero leaked tasks in every case, with and without the shield**,
because `_quiet_await`'s own `suppress(CancelledError)` already absorbs it.

The one path where it changes behaviour is a downgrade: with a non-`CancelledError` pending
plus a second cancel, `shield`'s `_inner_done_callback` calls `inner.exception()` purely to
mark it retrieved, so the error is **silently dropped** — masking [D4](#d4) in a narrow
window. It also costs an unconditional suspension (~3 loop iterations) on every request,
since `shield` always wraps the coroutine in a fresh Task.

---

<a id="d16"></a>
## D16 — Version-bounded and server-specific hazards — low

- **uvicorn 0.43.0–0.44.x** emit `http.disconnect` to every parked reader on server shutdown
  (`shutting_down` flag). Reverted in 0.45.0 (#2913). On those versions every in-flight
  request reports a false disconnect on SIGTERM.
- **granian h2** shares one `disconnect_guard` `Notify` per TCP connection, signalled with
  `notify_one()` — with N streams parked in `receive()`, only one is woken.
- **granian** loses an already-materialized chunk when a parked reader is cancelled
  (`src/callbacks.rs:481-497`, CAS against `Cancelled`). uvicorn, daphne and hypercorn are
  cancel-safe here.
- **hypercorn** bounds its app queue at `max_app_queue_size` (default **10**). If the watcher
  stops draining mid-upload the queue fills and `_send_closed()` blocks forever, stalling the
  connection — and on HTTP/2, every other stream on it.
- **WebSocket scopes** would spin: uvicorn returns `websocket.disconnect` immediately once
  closed, which never equals `http.disconnect`, and the fast path never awaits — a 100% CPU
  loop. Unreachable via `Request` (which asserts `scope["type"] == "http"`,
  `requests.py:213`), but the module's own tests pass a duck-typed `MockRequest`, so nothing
  structurally prevents it.

---

<a id="clean"></a>
# Checked and clean

Investigated specifically; these hold:

- **Dependency caching / dedup.** Cache key is `(call, sorted_scopes, computed_scope)`.
  `dependencies=[...]` at app + router + route + an `Annotated` param → **one** invocation,
  one watcher. `DisconnectEvent` + `DisconnectFencing` → two invocations, one shared watcher.
  `use_cache=False` twice → two invocations, still one watcher.
- **Solve and teardown order.** One request-scoped `AsyncExitStack`
  (`scope["fastapi_inner_astack"]`), depth-first declaration order, LIFO teardown — entered
  first ⇒ exits last, confirmed end-to-end.
- **Scope dict identity.** One object across `Mount`, `Router`, `Route.handle`,
  `BaseHTTPMiddleware`, `ExceptionMiddleware`, `ServerErrorMiddleware`. The only `dict(scope)`
  copies are `redirect_scope` / `base_url_scope`, used for matching and URL building only. So
  the scope cache genuinely yields one watcher per request, including across mounted sub-apps.
- **ContextVar propagation.** PEP 568 was never accepted, so async generators do *not* get an
  isolated context — `bind_fencing` inside a yield dependency mutates the caller's context,
  and set/reset happen in the same task, so `reset(token)` is always valid. Verified visible
  in the handler, later dependencies, the `StreamingResponse` body iterator, background tasks,
  and sync handlers in the threadpool. This is load-bearing and undocumented. Two narrow
  exceptions: outside `BaseHTTPMiddleware` (child anyio task), and
  `contextmanager_in_threadpool`, which enters and exits in two different context copies —
  meaning `bind_fencing`, a *sync* `@contextmanager`, must never be used inside a `def`
  dependency (`ValueError: <Token> was created in a different Context`). Not reachable today;
  the same error is reachable if a dependency generator is abandoned rather than closed
  (`async for ...: break`, or `loop.shutdown_asyncgens()`), which also leaks the binding.
- **`Request.receive` stability.** `_receive` is assigned once in `__init__` and never
  swapped; the property is a plain read. FastAPI passes one `Request` object to every
  dependency and the handler by identity.
- **Cancel-counter protocol.** No window exists where the teardown await lands inside a
  Fence's baseline: `bind_fencing` arms nothing, and `Fence.__exit__` → `resolve()` →
  `uncancel()` is fully synchronous, completing before the exit stack unwinds. After a
  suppressed disconnect cancel, `task.cancelling() == 0`.
- **Check-then-set in `_shared_disconnect_event`.** No `await` between `scope.get()` (`:102`)
  and the assignment (`:108`), and `@asynccontextmanager.__aenter__` runs to the `yield`
  without suspending — two concurrent tasks cannot both create a listener. The damage in
  [D3](#d3) comes from teardown ordering, not creation.
- **Message loss on cancelling a parked watcher.** Safe on uvicorn (no await between
  `message_event.clear()` and `return message`), and on daphne/hypercorn via
  `asyncio.Queue.get`'s documented cancel-safety. The loss in [D5](#d5) is *not* a
  cancellation artifact — the watcher discards while running normally.
- **Flow control.** `pause_reading()` fires only above the 64 KiB high-water mark or on
  pipelining, and both uvicorn impls unconditionally `resume_reading()` in
  `on_response_complete`. Cancelling the watcher cannot strand the transport. (Minor: the
  watcher's own `resume_reading()` defeats read backpressure for the request's duration and
  drains uploads at line rate.)
- **Error paths.** The `finally` at `starlette.py:113-115` was reached and the watcher awaited
  to completion in every case tested — handler raises, later dependency raises,
  `HTTPException`, outer `task.cancel()` mid-handler. `AsyncExitStack.__aexit__` is driven by
  `async with`, so there is no GC-instead-of-`aclose()` path.
- **Message types.** On an HTTP scope, all four servers emit only `http.request` and
  `http.disconnect`, always as a `dict`. No `None` sentinel, no `TypeError`/`KeyError` risk.

---

<a id="the-fix"></a>
# The fix

All channel conflicts in [Part A](#part-a) have one fix: **middleware that owns the receive
channel once and replays the disconnect downstream**, so every reader observes it. A
dependency cannot do this — Starlette hands `StreamingResponse` and `EventSourceResponse` the
raw `receive` captured in the route handler, before any dependency runs, so we never see the
reference we would need to wrap.

Three details worth lifting from `fastapi-disconnect`'s `CancelOnDisconnectMiddleware`:

1. **Track `response_complete` in the wrapped `send`.** uvicorn emits `http.disconnect` as
   soon as the response completes, so naive middleware cancels background tasks on every
   normal request. **This is [D1](#d1), and the current dependency has it today** — the
   middleware is not just a better shape, it is the only place the distinction can be drawn.
2. **Treat disconnect as a terminal side-channel**, not another item queued behind body
   chunks on a bounded queue. (See hypercorn's `max_app_queue_size=10` in [D16](#d16) for what
   a bounded queue does under load.)
3. **Replay the disconnect downstream** once the queue drains, so `StreamingResponse` and
   `EventSourceResponse` bodies that listen for it still work — resolving [D6](#d6) and
   [D7](#d7), and mattering most on the servers in [D8](#d8) that deliver it only once.

This would also remove the raw-body restriction, since the middleware can tee body chunks
rather than discard them — closing [D5](#d5).

**What middleware does not fix:** [D2](#d2) (code collapse) and [D3](#d3) (scope-cache
lifetime) are independent of the channel question and belong to the sharing design in
`749965d`. [D12](#d12) and [D13](#d13) are FastAPI structural facts. These need fixing on
their own terms.

---

<a id="status"></a>
## Status

`aiofence.contrib.middleware.DisconnectMiddleware` implements the above:
`_RequestChannel` is the single reader, body messages are replayed in order, the disconnect
is latched as a terminal side channel, and a wrapped `send` tracks `response_complete`.
`aiofence.contrib.starlette` borrows the published event when the middleware is installed and
keeps its own reference-counted watch when it is not — so installing it is a configuration
change, and the dependency-only column below is still reachable by choosing not to.

| # | Status |
|---|---|
| [D1](#d1) | **closed with the middleware.** `response_complete` is flipped before the terminal message reaches the server's `send`, so a disconnect latched afterwards is replayed downstream but never sets the event. Background tasks survive. Dependency-only: unchanged |
| [D2](#d2) | **closed.** `Fencing.event()` deduplicates on the `(event, code)` pair; every code reports independently. Independent of the middleware |
| [D3](#d3) | **closed.** The watch is reference counted and uncached before teardown; with the middleware there is no watch to outlive at all |
| [D4](#d4) | **closed.** The middleware latches the error, re-raises it from the next downstream `receive()`, never replaces the app's own exception, and logs at `WARNING` on `aiofence.contrib.middleware` if nothing ever reads. Dependency-only: logged and down, never re-raised at teardown. In both cases the event can no longer fire — inherent to a broken channel |
| [D5](#d5) | **closed with the middleware.** `http.request` messages are forwarded in order and unchanged; raw reads after any suspension return exact bytes. Dependency-only: unchanged |
| [D6](#d6) | **closed with the middleware.** Both readers are told on every `spec_version`. Which acts first is still scheduling — a *fenced* body deliberately outlives the rival listener's cancel scope, an unfenced one is still torn down by it. Dependency-only: unchanged |
| [D7](#d7) | **closed with the middleware.** `client_close_handler_callable` runs, pings stop, *and* `cancelled_by("disconnect")` is True. Dependency-only: unchanged, and now asserted as such in `tests/contrib/test_sse_starlette.py` |
| [D8](#d8) | **closed with the middleware.** One read of the server's channel, latched, then replayed to every later reader. Dependency-only: unchanged |
| [D9](#d9) | **closed with the middleware.** Installed outermost it owns the server's own `receive`; installed below a `BaseHTTPMiddleware` it owns `wrapped_receive` but is its only reader, so the non-reentrancy cannot be tripped from our side. There is nothing left to mask, since D1 is fixed. `WSGIMiddleware` still cannot host any of this |
| [D10](#d10) | **closed.** `pyproject.toml` carries `fastapi>=0.118` / `starlette>=0.42` extras |
| [D11](#d11) | **open without the middleware.** A function-scoped disconnect dependency still closes before the response and can strand a request-scoped one on an orphaned event. Moot with the middleware — no watch ownership, and the published event lives for the whole request |
| [D12](#d12) | **open, documented.** FastAPI structural: no running event loop in the threadpool, so a sync `def` handler cannot enter a fence |
| [D13](#d13) | **partially closed.** `DisconnectMiddleware(fencing_code=...)` binds outside the dependency stacks, so exception handlers see the fencing; `get_disconnect_event(scope)` always works there. A *dependency*-bound fencing is still gone by the time a handler runs, and the error-path teardown ordering still inverts versus the happy path |
| [D14](#d14) | **open by design, documented.** Both modules use `asyncio` primitives directly; aiofence is asyncio-only because `Fencing.event()` is typed against `asyncio.Event` |
| [D15](#d15) | **closed.** The inert `asyncio.shield` is gone from the teardown |
| [D16](#d16) | **partially closed.** hypercorn's `max_app_queue_size` is addressed — the pump drains unconditionally, which is why the body is buffered whether the app reads it or not. WebSocket spin is addressed: non-`http` scopes are passed through untouched with no scope key. Still open: uvicorn 0.43–0.44 waking every parked reader on shutdown (a pre-response disconnect, so it does set the event), granian h2's `notify_one()` across streams on one connection, and granian dropping a materialized chunk when a parked reader is cancelled |

Remaining costs of the middleware, both deliberate: one task per request, and the request body
buffered in memory for the request's lifetime, which defeats server read backpressure on large
uploads. See [api.md](api.md#what-it-costs).

---

# Documentation corrections required

1. `docs/api.md:350` — sync `def` handlers: the claim that `fence.cancelled_by(...)` "still
   reports correctly" is false; constructing a `Fence` raises. ([D12](#d12))
2. `docs/api.md:397` — "servers declaring 2.4 or higher skip Starlette's listener" is true but
   misleading: uvicorn ships 2.3, so the race is the default. ([D6](#d6))
3. `docs/api.md:365-374` — the raw-body caveat should name the actual unsafe shape
   (`Request`-only handlers, no declared body param) and state that the failure can be silent
   truncation to `b""`, not only a hang. ([D5](#d5))
4. `docs/api.md:280` — the scope cache is described without its lifetime contract: the entry
   outlives the watcher, the watcher is owned by the first entrant, and reuse is safe only
   while that entrant is on the stack. ([D3](#d3))
5. `docs/api.md:274-278` — `disconnect_fencing` "Creates an `asyncio.Event`"; it may reuse
   one. `starlette.py:38-40` says it "Builds on `disconnect_event`"; since `749965d` it builds
   on `_shared_disconnect_event`.
6. Undocumented entirely: [D1](#d1) (background tasks), [D2](#d2) (code collapse),
   [D4](#d4), [D13](#d13) (exception handlers), [D14](#d14) (asyncio-only), and the required
   FastAPI/Starlette version floor ([D10](#d10)).

---

# Test gaps

`MockRequest` (`tests/contrib/test_starlette.py:11-24`) and `scripted_receive`
(`tests/contrib/asgi_harness.py:41-50`) are more forgiving than any real server, and each
divergence hides a finding:

- **Never yields `http.request`** → the discard branch of the loop is never exercised;
  [D5](#d5) is structurally untestable.
- **No `response_complete` concept** → [D1](#d1) is invisible. Worse,
  `test__disconnect_event__when_body_completes__then_listener_cleaned_up` asserts
  `not event.is_set()` — exactly the assertion a real server fails.
- **Hangs after one disconnect** → cannot model uvicorn's repeated delivery, nor the one-shot
  starvation of [D8](#d8).
- **Never raises** → [D4](#d4) is untestable.
- **No second reader** and **no `asgi` key in the scope** → [D6](#d6), [D8](#d8), [D9](#d9)
  invisible. `test_sse_starlette.py` is the one place a real rival reader is exercised.
- **Unbounded queue** → no analogue of hypercorn's `max_app_queue_size=10`.

Specific test defects:

- `test__route_dependencies__when_also_declared_as_param__then_single_trigger` proves nothing
  about sharing: both `Depends(disconnect_fencing)` uses share a cache key, so FastAPI solves
  the dependency **once** (instrumented: `_shared_disconnect_event` entered 1 time). The test
  would pass identically if the shared-watcher code did not exist. A real test needs two
  *distinct* callables.
- Two tests named `..._then_listener_cleaned_up` never assert cleanup — only
  `not event.is_set()` / `not fence.cancelled`. Neither checks `listener.done()`, task counts,
  or scope-key release (it is not released).
- `bound_codes()` reads `Fencing._events`, which `event()` **prepends** to — so the list is
  reverse-insertion order. Every single-element assertion in `test_fastapi.py` is compatible
  with the [D2](#d2) collapse, which is why it reads as a plausible `["client_gone"]`.

**Missing tests:** sequential re-entry after teardown; two concurrent entrants; scope key
released on teardown; two different codes on one request; a `receive` that raises; teardown
while the enclosing task is cancelled; `DisconnectEvent` + `DisconnectFencing` on one handler;
sync `def` handler; a `receive` that returns `http.disconnect` on response completion.
