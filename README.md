<p align="center">
  <img src="docs/images/logo.png" alt="aiofence" />
</p>

# aiofence

[![codecov](https://codecov.io/gh/stanislaushimovolos/aiofence/branch/main/graph/badge.svg)](https://codecov.io/gh/stanislaushimovolos/aiofence)

Multi-reason cancellation for Python asyncio. A request rarely has one reason to stop: the client disconnects, the budget runs out, the service shuts down. Each arrives through a different mechanism, and handling them together means threading events, timeouts and flags through every call signature. Inspired by Go's `context.Context`, `aiofence` declares the sources once at the boundary and propagates them via `ContextVar` — inner code wraps cancellable work in a `Fence`, doesn't care about the actual reasons, and can ask afterwards which one fired.

**The flagship use case is client disconnect.** An inference or agent service burns GPU time and provider spend on requests nobody is listening to any more, and ASGI gives you exactly one shot at noticing. `DisconnectMiddleware` turns that one-shot signal into an ambient cancellation source, so any code below it can stop the work the moment the client goes away — with no `Request` in its signature and no wiring through the call stack. See [Client disconnects](#client-disconnects).

## Motivation

`asyncio` has been steadily adopting structured concurrency patterns — `TaskGroup` (3.11) and `asyncio.timeout()` (3.11) both came from `trio` and `anyio`. But one gap remains: `asyncio` can cancel tasks mechanically, but it can't tell you *why* you were cancelled, doesn't offer a non-raising timeout (`move_on_after`), and forces you to propagate cancellation sources through every call signature. When multiple sources exist (timeout, client disconnect, graceful shutdown), it gets messy fast:

```python
async def handle_request(request, shutdown_event, timeout=30):
    try:
        async with asyncio.timeout(timeout):
            while not shutdown_event.is_set():
                chunk = await get_next_chunk()
                if request.is_disconnected():
                    break
                await process(chunk)
    except TimeoutError:
        ...
    except asyncio.CancelledError:
        # shutdown? disconnect? something else?
        ...
```
For a deeper dive into the problem and design rationale, see [this Medium post](https://medium.com/p/8cdf8c5d519e).

`aiofence` solves this. Declare all cancellation sources once, composably. The callee doesn't even know cancellation exists:

```python
with (
    on_timeout(30)
    .event(shutdown, code="shutdown")
    .move_on_cancel()
) as fence:
    result = await fetch_and_transform()

if not fence.cancelled:
    await save(result)
else:
    print(fence.cancel_reasons)       # (CancelReason(message='timed out after 30s', ...),)
    print(fence.cancelled_by("shutdown"))  # True / False
```

Or raise instead of inspect:

```python
with on_timeout(30).raise_on_cancel() as fence:
    result = await fetch_and_transform()
# raises FenceCancelled if timed out
```

### What about `asyncio.shield()`?

`shield()` prevents cancellation from reaching shielded code, but it works from the opposite direction — you protect everything that *must not* be cancelled. In practice this means wrapping database writes, state transitions, logging, and cleanup individually, and each function needs to know whether it's cancel-safe.

`aiofence` comes at it differently: most code doesn't know cancellation exists. You only wrap the expensive, safely-interruptible parts — the operations you *want* to cancel. For example, in an LLM inference service, you don't want to cancel database queries or response formatting. You want to cancel the LLM call that's burning GPU time for a client that already disconnected:

```python
with (
    on_event(client_disconnect)
    .timeout(budget)
    .move_on_cancel()
) as fence:
    result = await llm.generate(prompt)  # cancellable

await db.save(result or fallback)  # always runs, no shield needed
```

### aiofence and anyio

`anyio.CancelScope` is the best cancellation *delivery* mechanism asyncio has: one scope, one deadline, one `cancel()`, shields honoured. What it does not do is the layer above delivery. It cannot say which of several sources fired, has no ambient "these are the cancellation sources for this request", no way to decline a reason under a precondition, and nothing for ASGI disconnects. `aiofence` is that layer, not a replacement: by default a fence cancels *through* an `anyio.CancelScope`, so the shields httpx and Starlette wrap their cleanup in hold.

The philosophies also differ, and compose. `anyio` puts one broad `CancelScope` over the operation and shields the parts that must survive. `aiofence` wraps only the expensive, safely interruptible part you *want* cancelled, and lets everything else run unaware. Inside a fence, library shields still hold.

## Features

**Composable triggers** — chain timeouts, events, deadlines, and custom triggers into a single `Fencing`. Each call returns a new immutable builder, so configs are safe to share and extend:

```python
fencing = on_timeout(30, code="budget").event(shutdown, code="shutdown")

# extend per-operation
with fencing.timeout(5, code="db").move_on_cancel() as fence:
    await query_db()
```

**Context propagation** — store a `Fencing` in a `ContextVar` at the boundary, read it anywhere with `get_current_fencing()`. No need to pass configs through every call signature:

```python
# HTTP handler boundary — a shared budget is a deadline
with bind_fencing(on_event(disconnect, code="disconnect").deadline(loop.time() + 30)):
    await handle_request()

# deep inside, no arguments needed
async def process():
    with get_current_fencing().move_on_cancel() as fence:
        await do_work()
```

**Typed cancellation reasons** — after cancellation, inspect *which* trigger fired. Each reason carries a machine-readable `code` for programmatic matching:

```python
if fence.cancelled_by("disconnect"):
    log("client left")
elif fence.cancelled_by("budget"):
    return cached_result
```

**Guarded cancellation** — a trigger firing is not always a reason to cancel. Decline a reason while a precondition holds, scoped to one code so the rest of the fence keeps working:

```python
with get_current_fencing().unless(generation.is_done, code="disconnect").move_on_cancel() as fence:
    async for chunk in upstream:   # keeps draining after the finish reason
        yield chunk
```

**Two delivery modes** — how the cancel reaches the task is a pluggable [backend](docs/api.md#cancel-backend); triggers, reasons and policy are the same either way. The two exist because two ecosystems disagree on what a cancel *is*.

- `AnyioBackend`, the default, opens a fresh `anyio.CancelScope` per fence and cancels through it. anyio is the backbone of Starlette and httpx, and their shields and locks only recognise a cancel anyio itself delivered ([httpcore `_synchronization.py`](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_synchronization.py#L190-L208)). A raw `task.cancel()` is foreign to them: inside a Starlette task group it can be suppressed, so a streaming generator keeps running after the client left; inside httpcore it can land on an anyio lock checkpoint mid state transition and leave a connection the pool never sweeps, one slot lost for the life of the pool. Cancelling *through* anyio keeps the fence inside the contract that code is written to, and is what lets fences nest.
- `NativeBackend` cancels with asyncio's own `task.cancel()` — edge-triggered, delivered exactly once, on the `cancel()`/`uncancel()` counter protocol. anyio instead re-cancels at every `await` until the scope exits ([`_deliver_cancellation`](https://github.com/agronholm/anyio/blob/4.12.1/src/anyio/_backends/_asyncio.py#L556-L594)), which breaks code written to asyncio's contract, such as catching `CancelledError` and awaiting a shielded task once more to drain it: the second `await` is cancelled too. Use this backend where the code under the fence is plain asyncio and expects a single cancel; it composes with `TaskGroup` and `asyncio.timeout()`.

Switch process-wide with `set_default_backend()`, per context with `bind_backend()`, or per fence with `Fence(backend=...)`.

## Client disconnects

For Starlette and FastAPI, this is what `aiofence` is mainly built for. `DisconnectMiddleware` owns the request's receive channel — one reader, replayed to everything below it — and binds its disconnect event to the current `Fencing` context via `bind_fencing()` for the whole request. Installing it is the whole setup: when the client disconnects, any fence created from `get_current_fencing()` — anywhere in the call stack — is cancelled with `code="disconnect"` (`DISCONNECT_CODE`). Declaring `DisconnectFencing` on a route is optional, and only needed for a per-route code:

```python
from starlette.middleware import Middleware
from aiofence.contrib.starlette import DISCONNECT_CODE, DisconnectMiddleware

app = FastAPI(middleware=[Middleware(DisconnectMiddleware)])   # outermost, required

@app.get("/work")
async def handler():
    with get_current_fencing().timeout(30, code="budget").move_on_cancel() as fence:
        await long_work()

    if fence.cancelled_by(DISCONNECT_CODE):
        return Response(status_code=499)
```

The real value is that the binding is ambient, so service-layer code doesn't need to know about HTTP, requests, or disconnect events — it reads the cancellation context via `get_current_fencing()`:

```python
# handler — no fencing wiring at the boundary either
@app.get("/generate")
async def handler(prompt: str):
    result = await generate_response(prompt)
    return {"status": "ok", "result": result}

# service layer — no request, no fencing in the signature
async def generate_response(prompt: str) -> str:
    # canceled on timeout or global disconnect event
    with (
        get_current_fencing()
        .timeout(30, code="budget")
        .move_on_cancel()
    ) as fence:
        result = await llm.generate(prompt)

    if fence.cancelled_by("disconnect"):
        return "client disconnected, skipping"
    if fence.cancelled_by("budget"):
        return await get_cached_response(prompt)
    return result
```

The disconnect is a signal, not an order. A streaming client routinely hangs up the moment it has the chunk it was waiting for — the finish reason, the tool call, the last token — while the provider still has one frame to send: with OpenAI-style streams the usage that gets billed arrives *after* the finish reason. Cancelling the upstream read there loses it. `unless()` declines the disconnect once the generation is past the point of cancelling, and the fence records which of the two happened:

```python
async def run_streaming(upstream, consume):
    fencing = get_current_fencing().unless(generation.is_done, code=DISCONNECT_CODE)
    try:
        with fencing.move_on_cancel() as fence:
            await consume(upstream)                   # keeps draining once is_done() flips
        if fence.cancelled_by(DISCONNECT_CODE):
            phase = "left while generating"          # upstream read cancelled, spend is partial
        elif fence.declined_by(DISCONNECT_CODE):
            phase = "left after the finish reason"   # drain ran to the end, usage is in
        else:
            phase = None
    finally:
        with anyio.CancelScope(shield=True):
            await close_upstream(upstream)            # runs whole on either outcome
            record(phase)
```

`unless()` is scoped to one code, so a timeout in the same fence still cancels. The shield in `finally` holds against Starlette's own teardown and any outer fence because both cancel through anyio; a raw `task.cancel()` would pass straight through it. Details in [Guarding cancellation](docs/api.md#guarding-cancellation).

Under the middleware every fence cancels through anyio, so a fence sits inside Starlette's and sse-starlette's task groups as one of their own scopes rather than as a foreign `task.cancel()`, and httpcore's shielded cleanup is left alone. Pass `DisconnectMiddleware(backend=NativeBackend())` to opt an app out; see [which backend cancels](docs/api.md#which-backend-cancels).

Code with no dependency and no `Request` to hand can read the event straight from the ambient request:

```python
from aiofence.contrib.starlette import get_disconnect_event

async def deep_helper():
    gone = get_disconnect_event()          # None when the middleware isn't installed
```

### Why this is the hard part

An ASGI receive channel has exactly one useful reader — `receive()` is a queue pop, not a broadcast — while a request routinely has several interested parties: `StreamingResponse`'s disconnect listener, sse-starlette's, `Request.is_disconnected()`, and your own code. Three properties make the arbitration correct, and hand-rolled watchers usually miss at least one:

- **One reader, above everything else.** On hypercorn, daphne and granian `http.disconnect` is delivered exactly once, so whoever reads it first consumes it and every other listener starves. A dependency cannot arbitrate — Starlette captures the raw `receive` before any dependency runs, so there is no reference left to wrap. Only a middleware sits above all of them.

- **Replay, don't discard.** The usual watcher loop drops everything that isn't a disconnect, which steals body chunks: the loser of that race gets `{"body": b"", "more_body": False}`, and Starlette accepts it as a *complete, empty* body. Silent truncation, no exception, no log. `DisconnectMiddleware` forwards every message downstream in order and unchanged. It does not make `Request.is_disconnected()` safe, though: that method pops and discards the next message whatever it is, body chunks included, so use the published event instead of polling it.

- **"Stream ended" is not "client left".** Per the ASGI spec `http.disconnect` means the stream ended, and every server sends it once the response is complete. A watcher that can't tell the two apart fires on every successful request — and takes `BackgroundTasks` down with it. The middleware tracks response completion in a wrapped `send`, so only a disconnect arriving *before* the response finished sets the event.

Full reasoning in [Disconnect Delivery — Design Rationale](docs/disconnect-watcher-analysis.md) and [Architecture](docs/architecture.md#disconnect-delivery-replay-and-record).

Requires `starlette` (installed with FastAPI) — `pip install aiofence[starlette]` or `aiofence[fastapi]`.

## Documentation

- [API Guide](docs/api.md) — usage, patterns, and examples
- [Architecture](docs/architecture.md) — how it works, cancellation flow, design decisions; [Cancel Backends](docs/architecture.md#cancel-backends) for native vs anyio delivery
- [Why Suppress](docs/why-suppress.md) — why `CancelledError` is suppressed instead of raised
- [Disconnect Delivery — Design Rationale](docs/disconnect-watcher-analysis.md) — why the middleware owns the receive channel, and why the dependencies have no fallback
- [CPython Task Cancellation](docs/cpython-task-cancellation.md) — how `asyncio.Task` cancellation works under the hood

## Caveats

**Nested Fences need the anyio backend**, which is the default. `NativeBackend` refuses a second `Fence` on the same task with `RuntimeError`; see [#12](https://github.com/stanislaushimovolos/aiofence/issues/12). Under anyio, scopes exit in strict LIFO order, so a fence must not span a `yield` in a generator.

**The disconnect dependencies require `DisconnectMiddleware`** and raise `RuntimeError` without it. There is no fallback on purpose — see [Why this is the hard part](#why-this-is-the-hard-part) and [the API guide](docs/api.md#why-the-middleware-is-required).

**Cancelling httpx requests can leak httpcore pool slots.** Two distinct ways, on upstream httpcore 1.0.9 (the latest release, April 2025). *Native-only:* a `task.cancel()` landing on the checkpoint inside an anyio lock — one loop tick right after connect, or right before a response is closed — leaves the connection stuck in `NEW` or `ACTIVE`, states the pool never sweeps; each hit is one slot gone for good. The window is a single tick per request, so for a multi-second upstream call the per-cancel odds are tiny, but the loss is permanent and cumulative. Under the default `AnyioBackend` this path is closed: anyio never delivers on that checkpoint, and it measured zero. *On either backend:* a saturated pool — every connection busy, requests queued — hits [encode/httpcore#961](https://github.com/encode/httpcore/issues/961), fix pending in [#986](https://github.com/encode/httpcore/pull/986): a queued request cancelled while being handed a fresh connection leaves that connection never connected and never swept. Below httpx's default of 100 connections per client ([`DEFAULT_LIMITS`](https://github.com/encode/httpx/blob/0.28.1/httpx/_config.py#L247)) this cannot happen; above it, it poisons the pool within seconds under load. [httpx2](https://pypi.org/project/httpx2/), Pydantic's maintained continuation, fixes the saturated case and takes the await out of the lock; what remains there is a native-only leak of pool request entries, not connections (the removal sits after the shielded close: [`PoolByteStream.aclose`](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_async/connection_pool.py#L409-L417), same shape in httpcore2). See [Cancel Backends](docs/architecture.md#cancel-backends).

## Requirements

Python 3.12+ and `anyio>=4.11` (the version that added `CancelScope.cancel(reason)`).

## License

MIT
