# CLAUDE.md

> Entry point for Claude Code when working with this repository.

Read and follow strictly: @CONTRIBUTING.md

## Project Context

Constellate is a Python asyncio-native cancellation context library, inspired by Go's `context.Context` with implicit propagation via `ContextVar`.

Unifies all cancellation reasons (timeout, client disconnect, manual cancel, parent cancelled) behind one interface. User code doesn't care *why* it was cancelled.

## Documentation

- @docs/architecture.md — architecture, core concepts, cancellation flow, design decisions

## Tech Stack

- **Python 3.12+** — asyncio-native, no threads
- **ContextVar** — implicit propagation (no `ctx` first-arg like Go)
- **anyio** — dependency for event loop compatibility
- **uv** — package management
- **hatchling** — build backend

## API Usage

```python
# build context (immutable, composable)
ctx = Context().with_timeout(5).with_cancel_on(disconnect_event)

# execute in scope
with Scope(ctx) as scope:
    await do_work()

if scope.cancelled:
    print(scope.reasons)  # [TimeoutExpired(5)]

# or construct directly
ctx = Context(TimeoutSource(30), EventCancelSource(shutdown_event))
```

## Workflow

### When to Ask
- **Git operations**: Ask before `push`, `force` commands, or operations affecting remote
- **Repeated test failures**: If tests fail 3+ times on the same issue, ask before continuing
- **Uncertain approach**: When multiple valid solutions exist, propose options first
