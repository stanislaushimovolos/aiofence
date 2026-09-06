# CLAUDE.md

> Entry point for Claude Code when working with this repository.

Read and follow strictly: @CONTRIBUTING.md

## Project Context

Multi-reason cancellation contexts for Python asyncio. Declare all cancellation sources once at the boundary — inner code wraps cancellable work in a `Fence` context manager. No need to thread events, flags, or tokens through every call signature.

For architecture, core concepts, cancellation flow, and design decisions see @docs/architecture.md

## Tech Stack

- **Python 3.12+** — asyncio-native, no dependencies, no threads
- **uv** — package management
- **hatchling** — build backend

## API Overview

```python
with Fence(deadline=loop.time() + 30, events=[(shutdown, "shutdown")]) as fence:
    await do_work()

if fence.cancelled:
    print(fence.cancel_reasons)       # (CancelReason(message='timed out after 30s', ...),)
    print(fence.cancelled_by("shutdown"))  # True / False

# decline a reason while a precondition holds; other codes still cancel
with get_current_fencing().unless(generation.is_done, code="disconnect").move_on_cancel() as fence:
    ...
```

Core types: `Fence`, `Fencing`, `CancelReason`, `CancelType`, `CancelPolicy`. A fence's sources are one absolute deadline and any number of `(event, code)` pairs.

## Workflow

### When to Ask
- **Git operations**: Ask before `push`, `force` commands, or operations affecting remote
- **Repeated test failures**: If tests fail 3+ times on the same issue, ask before continuing
- **Uncertain approach**: When multiple valid solutions exist, propose options first
