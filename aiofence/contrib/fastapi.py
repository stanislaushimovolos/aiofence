"""
Ready-made FastAPI dependency aliases.

``DisconnectEvent`` is an ``asyncio.Event`` set once the client goes away, for
handlers that would rather pick their own stopping point than be interrupted::

    @app.get("/search")
    async def handler(gone: DisconnectEvent):
        hits = []
        for shard in shards:
            if gone.is_set():
                break
            hits += await query(shard)
        return hits

``DisconnectFencing`` is a ``Fencing`` carrying a disconnect trigger, also bound
as the ambient context so anything the handler calls picks it up from
``get_current_fencing()``::

    @app.get("/render")
    async def handler(fencing: DisconnectFencing):
        with fencing.timeout(30, code="budget").move_on_cancel() as fence:
            frames = await render_scene()

        if fence.cancelled_by("disconnect"):
            return Response(status_code=499)

Handlers that never touch the value should skip the parameter and declare
``dependencies=[Depends(disconnect_fencing)]`` on the route, router, or app.

Requires ``fastapi``. For plain Starlette use ``aiofence.contrib.starlette``,
which these are built from. Both aliases inherit that module's receive-channel
restrictions — see the caveats in docs/api.md.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import Depends

from aiofence import Fencing

from .starlette import disconnect_event, disconnect_fencing

DisconnectEvent = Annotated[asyncio.Event, Depends(disconnect_event)]
DisconnectFencing = Annotated[Fencing, Depends(disconnect_fencing)]
