"""Capture of the printer Socket.IO traffic.

Verified on QA: Operator V2 opens ``wss://wss-qa.ninjavan.co/socket.io/?EIO=3&transport=websocket&auth=<token>``
and talks on namespace ``/printers``. A print is the page emitting
``42/printers,<ack>["message", <printer room>, {"event": "PRINTER:PRINT", "socket_id": ..., "jobs": [...],
"accessToken": ...}]``. The printer agent answers through the server with
``42/printers,["event", {"event": "PRINTER:UPDATE_JOB" | "PRINT:UPDATE_JOB", "status": ..., "tracking_id": ...}]``.

Frames are never logged raw: the namespace connect frame and PRINTER:PRINT carry the access token.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import BrowserContext, Page, WebSocket, WebSocketRoute

PRINTER_NAMESPACE = "/printers"
PRINT_EVENT = "PRINTER:PRINT"
JOB_UPDATE_EVENTS = ("PRINTER:UPDATE_JOB", "PRINT:UPDATE_JOB")
# A printer app's answer to the page's PRINTER:GATHER: {id: <room>, status: ONLINE|OFFLINE, name}. Operator V2
# closes and reopens its printer socket every ~2 s until one arrives, and only then sends print jobs.
APP_ANSWER_EVENT = "PRINTER:UPDATE"
SENSITIVE_KEY = re.compile(r"token|authorization|auth$|password|secret|cookie", re.I)

# Engine.IO "4" (message) + Socket.IO packet type, optional "/namespace," (connect frames may carry
# "?query" before the comma), optional ack id, then the JSON data. Same text format in EIO 3 and 4.
_SIO_PACKET = re.compile(r"^4([0-6])(/[^,\[{\"]*,?)?(\d*)(.+)?$", re.S)


@dataclass(frozen=True)
class SioPacket:
    type: int  # 0 CONNECT, 1 DISCONNECT, 2 EVENT, 3 ACK, 4 CONNECT_ERROR/ERROR
    namespace: str
    ack_id: str
    data: Any


def parse_sio_frame(payload: str | bytes) -> SioPacket | None:
    """Parse one Socket.IO text frame; None for pings, binary and anything that is not Socket.IO."""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    m = _SIO_PACKET.match(payload)
    if not m:
        return None
    namespace = (m.group(2) or "/").rstrip(",").split("?")[0] or "/"
    data = None
    if m.group(4):
        try:
            data = json.loads(m.group(4))
        except json.JSONDecodeError:
            return None
    return SioPacket(type=int(m.group(1)), namespace=namespace, ack_id=m.group(3), data=data)


def _walk_dicts(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_dicts(value)


def find_events(packet: SioPacket, names: tuple[str, ...]) -> list[dict[str, Any]]:
    """Every object in an EVENT packet whose ``event`` field is one of ``names`` (any nesting depth)."""
    if packet.type != 2:
        return []
    return [d for d in _walk_dicts(packet.data) if d.get("event") in names]


def job_list(message: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = message.get("jobs")
    if isinstance(jobs, dict):
        jobs = [jobs]
    return [j for j in jobs or [] if isinstance(j, dict)]


def job_tracking_ids(job: dict[str, Any]) -> tuple[str | None, str | None]:
    """(NV tracking ID, external/vendor tracking ID) of a relabel print job."""
    nv, ext = job.get("nv_tracking_id"), job.get("tracking_id")
    return (str(nv) if nv else None, str(ext) if ext else None)


def redact(obj: Any) -> Any:
    """Copy of a payload with credentials removed, safe to print or write to disk."""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if SENSITIVE_KEY.search(k) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def safe_socket_url(url: str) -> str:
    """Socket URL with everything but EIO/transport removed (the query carries the auth token)."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    kept = "&".join(f"{k}={query[k][0]}" for k in ("EIO", "transport") if k in query)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" + (f"?{kept}" if kept else "")


@dataclass
class PrintEvent:
    message: dict[str, Any]  # the full PRINTER:PRINT object as sent (contains the access token!)
    job: dict[str, Any]  # the job in it that belongs to the scanned tracking ID
    socket_url: str  # sanitized
    namespace: str
    received_at: str
    forwarded_to_printer: bool


@dataclass
class SocketState:
    urls: list[str] = field(default_factory=list)
    connected: bool = False
    connect_error: str | None = None
    socket_error: str | None = None
    open_sockets: int = 0
    all_closed_since: float | None = None  # monotonic time the last open printer socket closed

    def lost_for(self, seconds: float) -> bool:
        """True when no socket has been open for ``seconds`` (a routine close + reopen is not a loss)."""
        return self.all_closed_since is not None and time.monotonic() - self.all_closed_since > seconds


class PrinterSocketMonitor:
    """Watches the printer socket and resolves the PRINTER:PRINT event for one expected tracking ID.

    ``no_print=True`` proxies the socket instead of only watching it, and swallows PRINTER:PRINT so the
    job never reaches the printer agent (the page gets the ack it waits for).
    """

    def __init__(self, no_print: bool) -> None:
        self.no_print = no_print
        self.state = SocketState()
        self.ignored: list[str] = []  # tracking IDs of print events that were not ours
        self.duplicates = 0
        self.job_updates: dict[str, list[str]] = {}  # external tracking ID -> agent statuses
        self.app_answers: list[dict[str, Any]] = []  # PRINTER:UPDATE answers of printer apps, in order
        self._expected: tuple[str, asyncio.Future[PrintEvent]] | None = None

    async def attach(self, context: BrowserContext) -> None:
        if self.no_print:
            await context.route_web_socket(re.compile(r"/socket\.io/"), self._proxy)
        else:
            context.on("page", self._watch_page)
            for page in context.pages:
                self._watch_page(page)

    def expect(self, tracking_id: str) -> asyncio.Future[PrintEvent]:
        """Only events arriving after this call and carrying this tracking ID resolve the future."""
        future: asyncio.Future[PrintEvent] = asyncio.get_running_loop().create_future()
        self._expected = (tracking_id.upper(), future)
        return future

    # --------------------------------------------------------------------- transports

    def _watch_page(self, page: Page) -> None:
        page.on("websocket", self._on_websocket)

    def _on_websocket(self, ws: WebSocket) -> None:
        if "/socket.io/" not in ws.url:
            return
        url = safe_socket_url(ws.url)
        self._on_open()
        ws.on("framesent", lambda payload: self._from_page(payload, url, forwarded=True))
        ws.on("framereceived", lambda payload: self._from_server(payload, url))
        ws.on("socketerror", lambda error: self._on_socket_error(url, error))
        ws.on("close", lambda _ws: self._on_close())

    def _proxy(self, route: WebSocketRoute) -> None:
        url = safe_socket_url(route.url)
        server = route.connect_to_server()
        self._on_open()

        def from_page(message: str | bytes) -> None:
            packet = parse_sio_frame(message)
            if packet and packet.namespace == PRINTER_NAMESPACE and find_events(packet, (PRINT_EVENT,)):
                self._from_page(message, url, forwarded=False)
                if packet.ack_id:  # let the page's emit() resolve as if the server accepted it
                    route.send(f'43{packet.namespace},{packet.ack_id}[{{"success":true}}]')
                return
            server.send(message)

        def from_server(message: str | bytes) -> None:
            self._from_server(message, url)
            route.send(message)

        route.on_message(from_page)
        server.on_message(from_server)

    # --------------------------------------------------------------------- frames

    def _from_page(self, payload: str | bytes, url: str, forwarded: bool) -> None:
        packet = parse_sio_frame(payload)
        if not packet or packet.namespace != PRINTER_NAMESPACE:
            return
        if url not in self.state.urls:
            self.state.urls.append(url)
        for message in find_events(packet, (PRINT_EVENT,)):
            for job in job_list(message):
                self._on_print_job(message, job, url, packet.namespace, forwarded)

    def _from_server(self, payload: str | bytes, url: str) -> None:
        packet = parse_sio_frame(payload)
        if not packet or packet.namespace != PRINTER_NAMESPACE:
            return
        if url not in self.state.urls:
            self.state.urls.append(url)
        if packet.type == 0:
            self.state.connected = True
        elif packet.type == 4:
            data = packet.data
            message = data.get("message") if isinstance(data, dict) else data
            self.state.connect_error = str(message or "connection refused by server")
        for answer in find_events(packet, (APP_ANSWER_EVENT,)):
            self.app_answers.append({k: answer.get(k) for k in ("id", "status", "name")})
        for update in find_events(packet, JOB_UPDATE_EVENTS):
            status = f"{update['event']}={update.get('status')}"
            if update.get("error"):
                status += f" ({update['error']})"
            self.job_updates.setdefault(str(update.get("tracking_id") or ""), []).append(status)

    def _on_socket_error(self, url: str, error: str) -> None:
        self.state.socket_error = f"{url}: {error}"

    def _on_open(self) -> None:
        self.state.open_sockets += 1
        self.state.all_closed_since = None

    def _on_close(self) -> None:
        self.state.open_sockets = max(0, self.state.open_sockets - 1)
        if self.state.open_sockets == 0:
            self.state.all_closed_since = time.monotonic()

    def app_answer(self, room: str) -> dict[str, Any] | None:
        """The printer app's latest answer for this room (ONLINE preferred), if any."""
        mine = [a for a in self.app_answers if str(a.get("id") or "") == room]
        return next((a for a in reversed(mine) if a.get("status") == "ONLINE"), mine[-1] if mine else None)

    def _on_print_job(self, message: dict, job: dict, url: str, namespace: str, forwarded: bool) -> None:
        nv, ext = job_tracking_ids(job)
        expected = self._expected
        if expected is None or expected[0] not in {t.upper() for t in (nv, ext) if t}:
            self.ignored.append(nv or ext or "?")
            return
        future = expected[1]
        if future.done():
            self.duplicates += 1
            return
        future.set_result(
            PrintEvent(
                message=message,
                job=job,
                socket_url=url,
                namespace=namespace,
                received_at=datetime.now(timezone.utc).isoformat(),
                forwarded_to_printer=forwarded,
            )
        )
