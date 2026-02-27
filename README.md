<p align="center">
  <img src="docs/images/logo.png" alt="Constellate" />
</p>

# constellate

[![codecov](https://codecov.io/gh/stanislaushimovolos/constellate/branch/main/graph/badge.svg)](https://codecov.io/gh/stanislaushimovolos/constellate)

Multi-reason cancellation contexts for Python asyncio. Centralizes cancellation from multiple sources (timeouts, events, disconnects) into a single scope, so user code doesn't have to manage any of it.

## Motivation

asyncio can cancel tasks mechanically — `task.cancel()`, `asyncio.timeout()` — but it can't tell you *why* you were cancelled. When multiple cancellation sources exist (timeout, client disconnect, graceful shutdown), user code is forced to propagate events and flags through every call signature, spawn background listeners that watch for signals and cancel your task, and shield cleanup code so that a second cancel request doesn't kill the task mid-cleanup. Without a single centralized object that owns all of this, it gets messy fast:

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

Constellate solves this. Declare all cancellation sources once, composably. The callee doesn't even know cancellation exists:

```python
async def do_work():
    data = await fetch()
    result = await transform(data)
    await save(result)

with Fence(TimeoutTrigger(30), EventTrigger(shutdown)) as fence:
    await do_work()

if fence.cancelled:
    print(fence.reasons)       # [CancelReason(message='timed out after 30s', ...)]
    print(fence.cancelled_by("shutdown"))  # True / False
```

### What about `asyncio.shield()`?

`shield()` technically prevents cancellation, but it inverts the problem. Instead of marking the few heavy operations that *can* be cancelled, you end up wrapping every piece of business logic that *must not* be — database writes, state transitions, logging, cleanup. Every function has to decide for itself whether it's cancel-safe.

Constellate takes the opposite approach: most code doesn't know cancellation exists. You only wrap the expensive, safely-interruptible parts — the operations you *want* to cancel. For example, in an LLM inference service, you don't want to cancel database queries or response formatting. You want to cancel the LLM call that's burning GPU time for a client that already disconnected:

```python
with Fence(EventTrigger(client_disconnect), TimeoutTrigger(budget)) as fence:
    result = await llm.generate(prompt)  # cancellable

await db.save(result or fallback)  # always runs, no shield needed
```

### Why not anyio?

anyio is one of the greatest async libraries in the Python ecosystem, with genuinely brilliant solutions under the hood. That said, there are two reasons we built something separate:

1. **Adoption barrier.** anyio replaces asyncio's edge-triggered cancellation with its own `CancelScope` model — a more powerful and elegant approach. But if your app wasn't built on anyio from the start, adopting it means rewriting your entire async stack around these primitives. Most developers work with pure asyncio and don't expect that their task can be cancelled multiple times within nested scopes.

2. **It doesn't solve targeted cancellation.** Even with anyio, the pattern is the same — a broad `CancelScope` over the whole operation, with `CancelScope(shield=True)` around the parts that must survive. It's a different stack, but the same difficulty for our narrow problem. Constellate flips this: instead of shielding everything that *must not* be cancelled, you wrap only the few things that *can* be.

## How It Works

`Fence` is a sync context manager that arms triggers against the current asyncio task. When a trigger fires, the task is cancelled via asyncio's native `cancel()`/`uncancel()` counter protocol. On exit, the `CancelledError` is suppressed and the counter is balanced. The caller inspects `fence.cancelled` and `fence.reasons` after the block.

No custom exception types. No new runtime. No abstraction layer. Just asyncio's own machinery, used correctly.

## Requirements

Python 3.12+. No dependencies.

## License

MIT
