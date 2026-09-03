from .abc import CancelBackend, CancelHandle
from .native import NativeBackend

__all__ = [
    "CancelBackend",
    "CancelHandle",
    "NativeBackend",
    "get_default_backend",
    "set_default_backend",
]

_default_backend: CancelBackend = NativeBackend()


def set_default_backend(backend: CancelBackend) -> None:
    """
    Choose the backend every Fence built without an explicit ``backend``
    uses from now on — including those built by ``Fencing`` and the
    Starlette middleware. Call once at startup; process-wide.

    ``AnyioBackend`` lives in ``aiofence.backends.anyio`` and is not
    imported here so the base package stays free of anyio.
    """
    global _default_backend  # noqa: PLW0603
    _default_backend = backend


def get_default_backend() -> CancelBackend:
    return _default_backend
