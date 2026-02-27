# constellate

[![codecov](https://codecov.io/gh/stanislaushimovolos/constellate/branch/feat/graph/badge.svg)](https://codecov.io/gh/stanislaushimovolos/constellate)

Asyncio-native cancellation contexts for Python. Inspired by Go's `context.Context` with implicit propagation via `ContextVar`.

Unifies all cancellation reasons (timeout, client disconnect, manual cancel, parent cancelled) behind one interface. User code doesn't care *why* it was cancelled.

## License

MIT
