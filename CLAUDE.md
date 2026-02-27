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
with Fence(TimeoutTrigger(30), EventTrigger(shutdown, code="shutdown")) as fence:
    await do_work()

if fence.cancelled:
    print(fence.reasons)              # (CancelReason(message='timed out after 30s', ...),)
    print(fence.cancelled_by("shutdown"))  # True / False
```

Core types: `Fence`, `Trigger`, `TriggerHandle`, `CancelReason`, `CancelType`. Built-in triggers: `TimeoutTrigger`, `EventTrigger`.

## Workflow

### When to Ask
- **Git operations**: Ask before `push`, `force` commands, or operations affecting remote
- **Repeated test failures**: If tests fail 3+ times on the same issue, ask before continuing
- **Uncertain approach**: When multiple valid solutions exist, propose options first
