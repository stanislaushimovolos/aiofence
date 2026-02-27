# constellate

[![codecov](https://codecov.io/gh/stanislaushimovolos/constellate/branch/feat/initial-setup/graph/badge.svg)](https://codecov.io/gh/stanislaushimovolos/constellate)

Asyncio-native cancellation contexts for Python. Inspired by Go's `context.Context` with implicit propagation via `ContextVar`.

Unifies all cancellation reasons (timeout, client disconnect, manual cancel, parent cancelled) behind one interface. User code doesn't care *why* it was cancelled.

## Install

```bash
pip install constellate
```

## Usage

```python
import asyncio
from constellate import Fence, TimeoutTrigger, EventTrigger

# timeout-based cancellation
async def main():
    with Fence(TimeoutTrigger(5)) as fence:
        await do_work()

    if fence.cancelled:
        print(fence.reasons)  # [CancelReason(message='...', cancel_type=TIMEOUT)]

# event-based cancellation
async def with_shutdown(shutdown: asyncio.Event):
    with Fence(EventTrigger(shutdown)) as fence:
        await do_work()

# multiple triggers
async def combined(shutdown: asyncio.Event):
    with Fence(TimeoutTrigger(30), EventTrigger(shutdown)) as fence:
        await do_work()
```

## Key Concepts

- **`Trigger`** — abstract cancellation condition. Composable, immutable.
- **`Fence`** — sync context manager that arms triggers against the current task. Suppresses `CancelledError` on exit.
- **`CancelReason`** — tells you *why* cancellation happened (timeout, event, etc.).

## License

MIT
