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

Requires ``fastapi>=0.118`` (``pip install "aiofence[fastapi]"``); 0.106-0.117
tear yield dependencies down before the response is sent, which leaves the
watcher dead for the whole streaming phase.

Install ``aiofence.contrib.middleware.DisconnectMiddleware`` outermost. Without
it both aliases inherit the dependency's receive-channel restrictions, and the
disconnect signal also fires when the response completes — so anything running
after that point, ``BackgroundTasks`` in particular, sees an already-cancelled
fencing on every request. Sync (``def``) handlers cannot enter a fence either
way. See docs/disconnect-watcher-analysis.md.

For plain Starlette use ``aiofence.contrib.starlette``, which these are built
from.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import Depends

from aiofence import Fencing

from .starlette import disconnect_event, disconnect_fencing

DisconnectEvent = Annotated[asyncio.Event, Depends(disconnect_event)]
DisconnectFencing = Annotated[Fencing, Depends(disconnect_fencing)]
