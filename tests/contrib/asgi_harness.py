from __future__ import annotations

import asyncio
import json
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope

from aiofence import get_current_fencing


def http_scope(path: str = "/work", spec_version: str = "2.3") -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


def post_scope(content_length: int, path: str = "/work") -> Scope:
    return {
        **http_scope(path),
        "method": "POST",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(content_length).encode()),
        ],
    }


def scripted_receive(*messages: Message) -> Receive:
    pending = list(messages)

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        await asyncio.Event().wait()  # client stays connected, forever
        raise AssertionError  # pragma: no cover

    return receive


async def call_app(
    app: ASGIApp,
    *,
    scope: Scope | None = None,
    receive: Receive | None = None,
) -> Any:
    return json.loads(await call_app_body(app, scope=scope, receive=receive))


async def call_app_body(
    app: ASGIApp,
    *,
    scope: Scope | None = None,
    receive: Receive | None = None,
) -> bytes:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    # A stalled receive channel hangs rather than fails — bound it so CI can't block.
    async with asyncio.timeout(5):
        await app(scope or http_scope(), receive or scripted_receive(), send)

    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def bound_codes() -> list[str | None]:
    return [entry.code for entry in get_current_fencing()._events]
