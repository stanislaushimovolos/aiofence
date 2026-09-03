from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .abc import CancelBackend, CancelHandle
from .native import NativeBackend

__all__ = [
    "CancelBackend",
    "CancelHandle",
    "NativeBackend",
    "bind_backend",
    "get_default_backend",
    "set_default_backend",
]

_default_backend: CancelBackend = NativeBackend()
_bound_backend: ContextVar[CancelBackend | None] = ContextVar("aiofence_backend", default=None)


def set_default_backend(backend: CancelBackend) -> None:
    """
    Choose the backend every Fence built without an explicit ``backend``
    uses from now on, unless a ``bind_backend`` context says otherwise.
    Call once at startup; process-wide.

    ``AnyioBackend`` lives in ``aiofence.backends.anyio`` and is not
    imported here so the base package stays free of anyio.
    """
    global _default_backend  # noqa: PLW0603
    _default_backend = backend


@contextmanager
def bind_backend(backend: CancelBackend) -> Iterator[None]:
    """
    Use ``backend`` for every Fence built without an explicit one inside
    this context — in the current task and in tasks it spawns. Takes
    precedence over ``set_default_backend``. ``DisconnectMiddleware``
    binds its backend around each request this way.
    """
    token = _bound_backend.set(backend)
    try:
        yield
    finally:
        _bound_backend.reset(token)


def get_default_backend() -> CancelBackend:
    """
    The backend a Fence built here without an explicit one uses: the
    bound one if inside ``bind_backend``, else the process-wide default.
    """
    bound = _bound_backend.get()
    return bound if bound is not None else _default_backend
