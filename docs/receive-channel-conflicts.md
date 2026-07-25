# Receive Channel Conflicts

> Findings from investigating how `aiofence.contrib.starlette` interacts with the rest of the ASGI stack. Everything here was reproduced against FastAPI 0.140, Starlette 0.52.1, and sse-starlette 3.4.6.

## The rule

An ASGI receive channel has **exactly one useful reader**. `receive()` is a queue pop, not a broadcast: whichever caller is parked first gets the next message, and everyone else gets the ones after it. There is no way to peek, and no way to put a message back.

The disconnect watcher in `contrib/starlette.py` is a reader. It loops on `receive()` and discards everything that isn't `http.disconnect`. So every conflict below is the same conflict — something else in the stack also wants to read the channel, and the two split the message stream between them.

This is why `_shared_disconnect_event` caches its event in the ASGI scope. `disconnect_event` and `disconnect_fencing` on one endpoint used to start two watchers, and neither was guaranteed to see the disconnect. Inside our own module, one reader is something we can enforce. Outside it, it isn't.

## 1. Raw body reads

Reading the raw body while a fencing is bound races the watcher:

```python
@app.post("/upload", dependencies=[Depends(disconnect_fencing)])
async def handler(request: Request):
    await something()             # watcher parks in receive() here
    body = await request.body()   # may hang forever
```

If the watcher is parked first it drops the `http.request` chunk on the floor, and the body read waits for data that will never arrive.

**It is a race, not a deterministic hang.** Probing four cases:

| case | outcome |
|---|---|
| raw read immediately, no suspension | works — by luck |
| raw read after any `await` | **hangs** |
| FastAPI-parsed body param | safe |
| parsed param + suspension + explicit `request.body()` | safe |

The two safe rows have the same cause: FastAPI reads and caches the body at `routing.py:428/431`, *before* `solve_dependencies` at `:479`. Those messages are consumed before the watcher exists, and a later `request.body()` returns cached bytes without touching `receive()`.

The practical trap is that the first row passes in tests and hangs in production — under a real server the watcher is normally parked well before the body arrives over the network.

## 2. Starlette `StreamingResponse`

`StreamingResponse.__call__` starts its own `listen_for_disconnect` on the same channel, but only on one branch:

```python
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

**It turns on whether the handler suspended before returning the response.** With no `await` in the handler, the watcher task hasn't been scheduled yet when `StreamingResponse.__call__` calls `receive()`, so Starlette wins. Adding a single `await asyncio.sleep(0)` flips it. Reproduced in both directions.

### Teardown ordering is in our favour

Worth recording because it is the opposite of what you would guess, and the opposite of what `fastapi-disconnect`'s decorator suffers from: **FastAPI tears down `yield` dependencies after the response body has finished streaming.** Traced order for a streaming handler:

```
dep: setup → handler → send: response.start → body chunk1 → body chunk2 → send: body b'' → dep: teardown
```

So the `Fencing` context and the watcher are both alive for the whole stream. The binding is sound; only the second reader is a problem.

## 3. sse-starlette `EventSourceResponse`

The same conflict, deterministic, with the outcome reversed.

`EventSourceResponse` reads the channel **unconditionally** — there is no `2.4` escape hatch. This is deliberate, per its module docstring ("Divergence #1"): the loop also drives `client_close_handler_callable` and clears `self.active`, which is what stops `_ping` and the shutdown grace loop.

Its listener is started **last** (`# Wait for the client to disconnect last`), so our watcher — parked since dependency-solving time — wins consistently. Verified across all four combinations of handler-suspends × `spec_version`.

Measured against an unfenced control:

| | `client_close_handler` | pings after disconnect | generator |
|---|---|---|---|
| unfenced | fires | 1, then stops | aborted |
| fenced | **never fires** | 6, still going | runs to completion |

So fencing an SSE endpoint gets you working cancellation while silently switching off sse-starlette's own disconnect handling. For an LLM token-streaming endpoint that is a bad shape: cancellation appears to work, but a close handler doing cleanup, billing, or metrics never runs, and pings keep going to a closed socket.

### Aside: the shutdown watcher

sse-starlette starts one `_shutdown_watcher` task per *thread* (`threading.local`, not per event loop) on first use, and it outlives every request by design. Not a leak, but it trips strict per-test asyncio accounting — `tests/contrib/test_sse_starlette.py` cancels it per test rather than loosening the suite invariant. Note the `watcher_started` flag is only cleared in the watcher's `finally`, so a thread that reuses a fresh event loop can end up with the flag set and no live watcher.

## 4. Sync handlers

Not a channel conflict, but found alongside. FastAPI runs `def` handlers in a threadpool. The contextvar is copied into the thread, so `get_current_fencing()` returns the right `Fencing` — but entering a fence fails:

```
RuntimeError: no running event loop
```

It fails loudly at first use rather than silently never cancelling, so this needs documentation rather than a guard.

## The fix

All three channel conflicts have one fix: **middleware that owns the receive channel once and replays the disconnect downstream**, so every reader observes it. A dependency cannot do this — Starlette hands `StreamingResponse` and `EventSourceResponse` the raw `receive` captured in the route handler, before any dependency runs, so we never see the reference we would need to wrap.

Three details worth lifting from `fastapi-disconnect`'s `CancelOnDisconnectMiddleware`:

1. **Track `response_complete` in the wrapped `send`.** uvicorn emits `http.disconnect` as soon as the response completes, so naive middleware cancels background tasks on every normal request.
2. **Treat disconnect as a terminal side-channel**, not another item queued behind body chunks on a bounded queue.
3. **Replay the disconnect downstream** once the queue drains, so `StreamingResponse` and `EventSourceResponse` bodies that listen for it still work.

This would also remove the raw-body restriction, since the middleware can tee body chunks rather than discard them.
