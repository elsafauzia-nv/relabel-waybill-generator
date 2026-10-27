"""Browser automation for the Operator V2 Cross Border Relabel Tool.

Selectors and behaviour were verified against the live QA page (2026-10-21):
* the tool is a micro-frontend iframe (operator-react-qa.../order-management/cross-border-relabel-tool.html);
* vendor: antd select ``[data-testid="cross-border-relabel-tool-testid.vendor.select"]``, options
  ``.ant-select-item-option``; a country with one vendor (MY: JNT) is pre-selected;
* tracking ID: ``[data-testid="cross-border-relabel-tool-testid.nv-tid.input"]``; Enter submits the form;
* lookup: ``GET /<country>/3pl-oms/v1/external-xdock-order-mappings?tracking_id=...`` (404 = unknown ID);
* errors: antd notification in the iframe, ``.ant-notification-notice-message`` (shown for ~2 s), e.g.
  "<TID> not found" or "No relabel label found".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.async_api import BrowserContext, Error as PlaywrightError, Frame, Page, Playwright, Request, Response

import report
from errors import Failure, RunError

TOOL_URL = "https://operatorv2-qa.ninjavan.co/react/#/{country}/cross-border-relabel-tool"
MFE_FRAME = re.compile(r"cross-border-relabel-tool\.html")
VENDOR_SELECT = '[data-testid="cross-border-relabel-tool-testid.vendor.select"]'
VENDOR_OPTION = ".ant-select-item-option"
TID_INPUT = '[data-testid="cross-border-relabel-tool-testid.nv-tid.input"]'
MAPPING_API = "3pl-oms/v1/external-xdock-order-mappings"

# Records every antd notification the relabel tool shows (they vanish after ~2 s).
_ERROR_OBSERVER_JS = """() => {
  if (window.__waybillNotices) { window.__waybillNotices.length = 0; return; }
  window.__waybillNotices = [];
  const seen = new WeakSet();
  document.querySelectorAll('.ant-notification-notice').forEach(n => seen.add(n));
  const record = n => setTimeout(() => {
    const msg = n.querySelector('.ant-notification-notice-message');
    const desc = n.querySelector('.ant-notification-notice-description');
    const text = [msg && msg.innerText, desc && desc.innerText].filter(Boolean).join(' - ').trim();
    if (text) window.__waybillNotices.push({ text, error: /error/.test(n.className) });
  }, 100);
  const scan = node => {
    if (!(node instanceof HTMLElement)) return;
    const notices = node.matches('.ant-notification-notice') ? [node] : [...node.querySelectorAll('.ant-notification-notice')];
    notices.forEach(n => { if (!seen.has(n)) { seen.add(n); record(n); } });
  };
  new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(scan)))
    .observe(document.body, { childList: true, subtree: true });
}"""


# ------------------------------------------------------------------------- session


async def launch_context(pw: Playwright, profile: Path, channel: str | None) -> BrowserContext:
    """Persistent Chromium profile so the Operator V2 sign-in survives between runs."""
    launch: dict[str, Any] = dict(
        user_data_dir=str(profile),
        headless=False,
        viewport=None,
        # Google SSO refuses browsers that announce automation.
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled"],
    )
    if channel:
        try:
            return await pw.chromium.launch_persistent_context(channel=channel, **launch)
        except PlaywrightError:
            report.get().line("info", f"({channel} not available, using Playwright's Chromium)")
    return await pw.chromium.launch_persistent_context(**launch)


async def restore_cookies(context: BrowserContext, cookie_file: Path) -> None:
    """Operator V2's auth cookie is a session cookie, which Chrome drops on exit; put it back."""
    try:
        cookies = json.loads(cookie_file.read_text())
    except (OSError, json.JSONDecodeError):
        return
    now = time.time()
    with contextlib.suppress(PlaywrightError):
        await context.add_cookies([c for c in cookies if c.get("expires", -1) in (-1, None) or c["expires"] > now])


async def save_cookies(context: BrowserContext, cookie_file: Path) -> None:
    cookies = [c for c in await context.cookies() if "ninjavan" in c.get("domain", "")]
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    cookie_file.write_text(json.dumps(cookies))
    cookie_file.chmod(0o600)


async def open_relabel_tool(context: BrowserContext, country: str, login_timeout: float, cookie_file: Path) -> Page:
    """Open the tool; if Operator V2 asks for sign-in, wait for the user to do it, then continue."""
    url = TOOL_URL.format(country=country.lower())
    page = context.pages[0] if context.pages else await context.new_page()
    await restore_cookies(context, cookie_file)
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_function(
        "location.hash.includes('/login') || !!document.querySelector(\"iframe[src*='cross-border-relabel-tool']\")",
        timeout=60_000,
    )
    if "/login" in page.url:
        report.get().line("info", "Not signed in. Sign in to Operator V2 in the browser window; the run continues by itself afterwards.")
        report.get().stage("browser", "running", "Waiting for you to sign in to Operator V2 in the browser window")
        deadline = time.monotonic() + login_timeout
        # Poll the URL rather than waiting in-page: Google SSO navigates away and back.
        while not ("operatorv2" in page.url and "/login" not in page.url):
            if page.is_closed():
                raise RunError(Failure.AUTH, "the browser window was closed before signing in")
            if time.monotonic() > deadline:
                raise RunError(Failure.AUTH, f"not signed in within {login_timeout:.0f}s", "run again and sign in")
            await asyncio.sleep(1)
        report.get().line("info", "Signed in.")
        await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("iframe[src*='cross-border-relabel-tool']", timeout=60_000)
    except PlaywrightError:
        raise RunError(
            Failure.BROWSER,
            f"the Cross Border Relabel Tool did not load for {country}",
            "check that your Operator V2 user can open this page for this country",
        ) from None
    await save_cookies(context, cookie_file)
    return page


async def wait_for_blocking_dialog(page: Page, timeout: float) -> None:
    """Operator V2 can open a modal over the tool that blocks every click; wait for the user to close it."""
    portal = page.locator("#headlessui-portal-root")
    deadline = time.monotonic() + timeout
    announced = False
    while await portal.count() and await portal.first.is_visible():
        if not announced:
            text = " ".join((await portal.first.inner_text()).split())[:200]
            report.get().line("info", f"A dialog is open in Operator V2 and blocks the tool - please close it: {text or '(no text)'}")
            report.get().stage("submit", "running", "A dialog in Operator V2 blocks the tool - close it in the browser window")
            announced = True
        if time.monotonic() > deadline:
            raise RunError(Failure.BROWSER, "an Operator V2 dialog is still blocking the tool")
        await asyncio.sleep(1)


async def relabel_frame(page: Page) -> Frame:
    for _ in range(240):
        frame = next((f for f in page.frames if MFE_FRAME.search(f.url)), None)
        if frame:
            await frame.wait_for_load_state("domcontentloaded")
            await frame.locator(TID_INPUT).first.wait_for(state="attached", timeout=30_000)
            return frame
        await asyncio.sleep(0.25)
    raise RunError(Failure.BROWSER, "the relabel tool iframe did not load")


async def read_printer_config(page: Page) -> dict[str, Any] | None:
    """Operator V2's active printer for this session (localStorage 'printer'): name, version, address."""
    raw = await page.evaluate("() => localStorage.getItem('printer')")
    try:
        printer = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None
    if not isinstance(printer, dict):
        return None
    address = str(printer.get("address") or printer.get("ip_address") or "")
    return {"name": printer.get("name"), "version": printer.get("version"), "address": address,
            "address_scheme": address.split(":")[0]}


# ------------------------------------------------------------------------- tool UI


async def select_vendor(frame: Frame, vendor: str) -> None:
    select = frame.locator(VENDOR_SELECT).first
    await select.wait_for(state="visible", timeout=15_000)
    if vendor in (await select.inner_text()).split():
        return  # already selected (single-vendor countries are pre-selected by the tool)
    await select.click(timeout=15_000)
    options = frame.locator(VENDOR_OPTION)
    await options.first.wait_for(state="visible", timeout=5_000)
    names = [" ".join(t.split()) for t in await options.all_inner_texts()]
    if vendor not in names:
        await select.press("Escape")
        raise RunError(Failure.INPUT, f"the tool does not offer vendor {vendor} here (offers: {', '.join(names)})")
    await options.filter(has_text=re.compile(rf"^\s*{re.escape(vendor)}\s*$")).first.click(timeout=5_000)
    if vendor not in (await select.inner_text()).split():
        raise RunError(Failure.BROWSER, f"vendor {vendor} could not be selected")


async def watch_tool_messages(frame: Frame) -> None:
    """Start (or reset) recording the tool's notifications."""
    await frame.evaluate(_ERROR_OBSERVER_JS)


async def tool_messages(frame: Frame) -> list[dict[str, Any]]:
    with contextlib.suppress(PlaywrightError):
        return list(await frame.evaluate("() => window.__waybillNotices || []"))
    return []


async def print_log_status(frame: Frame, tracking_id: str) -> str | None:
    """Status of the newest print-log row for this tracking ID: "Printing", "Success", or an error text.

    The tool's print log is a grid of three cells per row - NV tracking ID, external ID, status -
    newest row first.
    """
    with contextlib.suppress(PlaywrightError):
        return await frame.evaluate(
            """(tid) => {
                for (const cell of document.querySelectorAll('div')) {
                    if (cell.children.length || cell.textContent.trim() !== tid) continue;
                    const status = cell.nextElementSibling && cell.nextElementSibling.nextElementSibling;
                    if (status) return status.innerText.trim();
                }
                return null;
            }""",
            tracking_id,
        )
    return None


async def scan_tracking_id(frame: Frame, tracking_id: str) -> None:
    """Type the tracking ID into the tool and submit it exactly once (Enter), like a scanner."""
    tid_input = frame.locator(TID_INPUT).locator("xpath=descendant-or-self::input").first
    await tid_input.wait_for(state="visible", timeout=15_000)
    if await tid_input.is_disabled():
        raise RunError(Failure.BROWSER, "the tracking ID input is disabled (no vendor selected)")
    await tid_input.fill(tracking_id)
    await tid_input.press("Enter")


# ------------------------------------------------------------------------- lookup API


@dataclass
class Lookup:
    status: int | None = None
    api_message: str | None = None
    vendor: str | None = None
    external_tracking_id: str | None = None
    empty: bool = False
    network_error: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


class LookupWatcher:
    """Watches the tool's mapping lookup for the scanned tracking ID."""

    def __init__(self) -> None:
        self._tracking_id: str | None = None
        self.lookup = Lookup()

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)

    def expect(self, tracking_id: str) -> Lookup:
        self._tracking_id = tracking_id.upper()
        self.lookup = Lookup()
        return self.lookup

    def _matches(self, request: Request) -> bool:
        if MAPPING_API not in request.url or not self._tracking_id:
            return False
        tid = (parse_qs(urlparse(request.url).query).get("tracking_id") or [""])[0]
        return tid.upper() == self._tracking_id

    def _on_response(self, response: Response) -> None:
        if self._matches(response.request):
            asyncio.ensure_future(self._read(response, self.lookup))

    async def _read(self, response: Response, lookup: Lookup) -> None:
        lookup.status = response.status
        try:
            body = await response.json()
        except Exception:
            body = None
        if response.status >= 400:
            error = body.get("error") if isinstance(body, dict) else None
            lookup.api_message = (error.get("message") if isinstance(error, dict) else None) or (
                json.dumps(body)[:300] if body else None
            )
        else:
            mapping = body.get("data", body) if isinstance(body, dict) else None
            if isinstance(mapping, dict) and mapping:
                lookup.vendor = mapping.get("type")
                lookup.external_tracking_id = mapping.get("external_tracking_id")
            else:
                lookup.empty = True
        lookup.done.set()

    def _on_request_failed(self, request: Request) -> None:
        if self._matches(request):
            self.lookup.network_error = request.failure or "request failed"
            self.lookup.done.set()
