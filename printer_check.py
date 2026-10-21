"""Verify, before any scan, that Operator V2 will print to this computer's printer app.

Checked against three sources (verified on QA, 2026-10-21); nothing is changed by this script:

* this computer's printer: the room id of the local nv-label-printer app
  (~/Library/Application Support/nv-label-printer/storage/identifier.json, e.g. "rtwowbvm" -> wss:rtwowbvm);
* Operator V2 - Printers (#/<country>/printers): "Current active printer: <name> on <address>" and the
  printer list the page loads (GET /<country>/printer/printers?only_default=false), whose ``is_default``
  is the page's "Is Default Printer?" column;
* the printer the relabel tool will use in this browser session (Operator V2's localStorage 'printer').

The scan only goes ahead when all of them point to this computer's printer and its "Is Default Printer?"
is true. Otherwise the print job would go to another printer's room - e.g. the socket closes during the
scan ("printer WebSocket failure ... socket closed").
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Error as PlaywrightError, Response

from errors import Failure, RunError

APP_STORAGE = Path.home() / "Library/Application Support/nv-label-printer/storage"
PRINTERS_URL = "https://operatorv2-qa.ninjavan.co/react/#/{country}/printers"
_ACTIVE = re.compile(r"Current active printer:\s*(.+?)\s+on\s+(\S+)")
_HINT = (
    "on Operator V2 - Printers (https://operatorv2-qa.ninjavan.co/react/#/{country}/printers), make "
    "{local} - this computer's printer app - the active printer with 'Is Default Printer?' = true, "
    "then scan again. Note: only one printer in the list is the default, so another QA member's "
    "printer may currently hold it."
)


def local_printer_room() -> str | None:
    """Room id of the nv-label-printer app on this computer (its 'identifier')."""
    try:
        value = json.loads((APP_STORAGE / "identifier.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return str(value) if value else None


def _bare(address: Any) -> str:
    return str(address or "").strip().removeprefix("wss:")


@dataclass
class PrintersPage:
    active_name: str | None
    active_address: str | None
    printers: list[dict[str, Any]]  # name, address, version, is_default


async def read_printers_page(context: BrowserContext, country: str) -> PrintersPage:
    """Open Operator V2 - Printers in a new tab; read the active printer and the printer list it loads."""
    page = await context.new_page()
    listing: asyncio.Future[list[dict[str, Any]]] = asyncio.get_running_loop().create_future()

    async def on_response(response: Response) -> None:
        if "printer/printers" in response.url and "only_default=false" in response.url and response.status == 200:
            try:
                body = await response.json()
            except Exception:
                return
            rows = body.get("data") if isinstance(body, dict) else None
            if isinstance(rows, list) and not listing.done():
                listing.set_result([
                    {k: row.get(k) for k in ("name", "address", "version", "is_default")}
                    for row in rows if isinstance(row, dict)
                ])

    def listener(response: Response) -> None:
        asyncio.ensure_future(on_response(response))

    page.on("response", listener)
    try:
        await page.goto(PRINTERS_URL.format(country=country.lower()), wait_until="domcontentloaded")
        printers = await asyncio.wait_for(listing, 30)
        frame = None
        for _ in range(120):
            frame = next((f for f in page.frames if "/printer/index.html" in f.url), None)
            if frame:
                break
            await asyncio.sleep(0.25)
        active = None
        if frame:
            await frame.get_by_text("Current active printer").first.wait_for(timeout=20_000)
            active = _ACTIVE.search(await frame.locator("body").inner_text())
        return PrintersPage(active.group(1) if active else None, active.group(2) if active else None, printers)
    except (asyncio.TimeoutError, PlaywrightError) as e:
        detail = "the printer list did not load" if isinstance(e, asyncio.TimeoutError) else str(e).splitlines()[0]
        raise RunError(Failure.PRINTER_MISMATCH, f"could not read Operator V2 - Printers ({detail})") from None
    finally:
        page.remove_listener("response", listener)
        await page.close()


def verify(country: str, local_room: str | None, printers_page: PrintersPage, session_printer: dict | None) -> str:
    """Raise RunError(PRINTER_MISMATCH) unless everything points to this computer's printer. Returns a summary."""
    if not local_room:
        raise RunError(
            Failure.PRINTER_MISMATCH,
            "this computer's printer app id was not found (nv-label-printer is not installed here, or has "
            "never been signed in)",
            "install nv-label-printer, open it and sign it in (Operator V2 Settings -> Open label printer)",
        )
    local = f"wss:{local_room}"
    hint = _HINT.format(country=country.lower(), local=local)
    entries = [p for p in printers_page.printers if _bare(p.get("address")) == local_room]
    if not entries:
        raise RunError(
            Failure.PRINTER_MISMATCH,
            f"this computer's printer {local} is not listed on Operator V2 - Printers",
            f"add it on Operator V2 - Printers (Add printer, address {local}), then: " + hint,
        )
    entry = next((p for p in entries if p.get("is_default") is True), entries[0])
    problems = []
    if _bare(printers_page.active_address) != local_room:
        problems.append(f"Operator V2's current active printer is '{printers_page.active_name}' on "
                        f"{printers_page.active_address}, not this computer's printer {local}")
    if entry.get("is_default") is not True:
        default = next((p for p in printers_page.printers if p.get("is_default") is True), None)
        holder = f" (the default is '{default.get('name')}' on {default.get('address')})" if default else ""
        problems.append(f"'Is Default Printer?' of '{entry.get('name')}' {local} is {entry.get('is_default')}{holder}")
    session_address = (session_printer or {}).get("address")
    if session_address and _bare(session_address) != local_room:
        problems.append(f"this browser session would print to '{(session_printer or {}).get('name')}' on "
                        f"{session_address}")
    if problems:
        raise RunError(Failure.PRINTER_MISMATCH, "; ".join(problems), hint)
    return f"'{entry.get('name')}' {local} - this computer's printer app, the active printer, Is Default Printer? true"
