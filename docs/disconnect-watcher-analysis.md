# Disconnect Delivery — Design Rationale

> Why `DisconnectMiddleware` owns the request's receive channel, and why the disconnect
> dependencies have no fallback.

## Two properties of the ASGI receive channel

Everything else follows from these.

**1. The channel has exactly one useful reader.** `receive()` is a queue pop, not a
broadcast. There is no peek and no put-back. But a request routinely has several interested
parties — the disconnect dependency, `StreamingResponse.listen_for_disconnect`,
sse-starlette's listener, `Request.is_disconnected()`, and the body reader. Whoever reads
first consumes the message; on hypercorn, daphne and granian each message is delivered
exactly once, so everyone else starves.

**2. `http.disconnect` does not mean "the client disconnected".** Per the ASGI spec it is
sent "if receive is called after a response has been sent **or** after the connection has
been closed" — a *stream-ended* signal, not a *client-gone* one. Every server collapses the
two into the same message. Read naively, it fires on every successful request.

## Why a middleware, and not a dependency

A dependency cannot arbitrate reader conflicts, because Starlette captures the raw `receive`
and hands it to `StreamingResponse` / `EventSourceResponse` before any dependency runs —
there is no reference left to wrap. Only a middleware sits above the whole stack. It reads
the channel exactly once and replays what it read to everyone below.

## What the middleware does, and why each part is load-bearing

- **Replay, don't discard.** Every `http.request` message is forwarded downstream in order
  and unchanged. A watcher that discards non-disconnect messages steals body chunks, and the
  loser of that race receives `{"body": b"", "more_body": False}` — which Starlette accepts
  as a *complete, empty* body. Silent truncation: no exception, no log.
- **Record, don't queue.** The disconnect is recorded once and answered on every later
  `receive()` — recorded rather than queued as a message the first reader would consume.
  Buffered body messages are still handed over first; the event itself is set at once. That turns a one-shot server delivery into a
  signal every reader below can observe. Draining the server's queue matters in itself —
  hypercorn bounds its app queue at 10 messages and blocks the connection when it fills.
- **Track response completion in a wrapped `send`.** This is the only place in the stack
  where property 2's two meanings can be told apart. The flag flips *before* the terminal
  message reaches the server's `send`, because the read loop can only be woken by the server
  having processed it. A disconnect recorded afterwards is replayed downstream but does not
  set the event — which is what keeps `BackgroundTasks` alive on successful requests. When
  `http.response.start` declares `"trailers": True`, the response ends at the final
  `http.response.trailers`, not the final body message.
- **Take `send` errors as a disconnect.** From ASGI spec 2.4 a server may raise a subclass
  of `OSError` from `send` on a closed connection, and the spec warns it can raise before the
  disconnect message reaches a reader. On the terminal message that would beat the completion
  flag to the answer, so the wrapped `send` sets the event itself and re-raises. No phase
  check: nothing follows the terminal message, so a refused send is a response the client
  never got.
- **Record `receive` errors.** A raising channel is re-raised from the next downstream
  `receive()` once the buffered body is drained, where the application actually reads, and never replaces the application's own
  exception. The event is *not* set: a broken channel is not evidence the client left.

The read loop stops as soon as it records a disconnect — uvicorn re-delivers the message
immediately and forever, so reading past it would spin for the rest of the request.

Replay settles that both readers are *told*, not which acts first. A fenced streaming body
can therefore outlive its rival listener's cancel scope: `move_on_cancel()` suppressed the
cancellation deliberately, so the generator resumes and emits its last chunk.

## Why there is no fallback

The dependencies raise `RuntimeError` when the middleware is absent rather than watching the
channel themselves. Every failure below is a property of *being a second reader*, so a
degraded mode would only be a quieter bug.

## What it costs

One task per request, and the request body is buffered in memory for the request's lifetime
whether or not the application reads it. Draining unconditionally is what keeps a bounded
server queue from stalling the connection, but it does defeat read backpressure on large
uploads.

## Findings index

What a dependency-owned watcher does wrong. Kept as IDs because the test suite references
them by number; the full reproductions are in git history at `4e6f414`.

| # | Finding | Status |
|---|---|---|
| D1 | `http.disconnect` also means "response finished" — false disconnect on every successful request, kills `BackgroundTasks` | closed |
| D2 | Two disconnect codes on one request collapse to one | closed — `Fencing.event()` dedups on `(event, code)` |
| D3 | Scope-cached event outlives the watcher it points at | closed — no watch to outlive |
| D4 | Watcher exceptions invisible, then land after the response was sent | closed |
| D5 | Body theft — watcher eats `http.request`; raw reads hang or return `b""` | closed |
| D6 | `StreamingResponse` rival reader; the "spec 2.4 is safe" escape hatch almost never applies | closed |
| D7 | sse-starlette — we win deterministically and silently disable its close handler | closed |
| D8 | Non-uvicorn servers deliver `http.disconnect` exactly once — starvation | closed |
| D9 | `BaseHTTPMiddleware`'s `wrapped_receive` is non-reentrant; also masks D1 | closed |
| D10 | No version floor for the teardown ordering the design relies on | closed — `fastapi>=0.118` / `starlette>=0.42` |
| D11 | `Depends(..., scope="function")` inverts the LIFO invariant | moot — dependencies own no watch |
| D12 | Sync `def` handlers cannot enter a fence | **open, documented** — FastAPI structural |
| D13 | Exception handlers see no *dependency-bound* fencing | closed — the middleware binds the fencing itself by default |
| D14 | asyncio-only primitives, no Trio guard | **open by design** — `Fencing.event()` is typed against `asyncio.Event` |
| D15 | `asyncio.shield` in teardown was inert and hid one error path | closed |
| D16 | Version-bounded and server-specific hazards | **partial** — see below |
| D17 | Response trailers move the end of the response past the final body message | closed |
| D18 | ASGI 2.4 `send` raises on a closed connection, possibly before the disconnect message — the completion flag would mask it | closed |

## Still open

- **D16 residue**, all server-internal: uvicorn 0.43–0.44 waking every parked reader on
  shutdown, granian h2 sharing one `notify_one()` across streams on a connection, and granian
  dropping a materialized chunk when a parked reader is cancelled.
- **Untestable here**, needing another event loop or a server nobody ships: D9 reentrancy via
  user middleware that reads the body after `call_next`, D14 under Trio, and D17 against a
  server that actually implements the trailers extension. `tests/contrib/live` now removes the
  "real server" half of that blocker; D9 is reachable there and simply not written yet.

## Environment

Python 3.12 · Starlette 0.52.1 · FastAPI 0.140.0 · anyio 4.12.1 · sse-starlette 3.4.6.
Server behaviour cross-checked against uvicorn 0.51.0, hypercorn 0.18.0, daphne 4.2.3,
granian 2.7.9, and the ASGI HTTP spec.

The uvicorn and hypercorn halves of that reading are no longer only a reading. `tests/contrib/live`
runs both servers in-process over a real socket, and `test_server_contract.py` asserts the two
things `FakeServer` is built on: hypercorn delivers `http.disconnect` once where uvicorn repeats
it, and neither advertises `spec_version` 2.4. A release that changes either fails there, instead
of leaving the fake quietly modelling a server that no longer exists. Everything else in that
directory is end-to-end — a real TCP abort, real chunked framing, the real ordering between a
completed response and the disconnect that follows it. daphne and granian stay source-read only.

No server advertises HTTP `spec_version` 2.4 today — uvicorn raised `ClientDisconnected` from
`send` in 0.27.0/0.28.0, reverted it in 0.28.1 and dropped back to 2.3 in 0.32.1; hypercorn
sends 2.1. D18 is therefore covered by the harness (`FakeServer(raise_on_send=True)`) rather
than by a live server.
