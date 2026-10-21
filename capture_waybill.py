#!/usr/bin/env python3
"""Generate a waybill through the Operator V2 QA Cross Border Relabel Tool from its real print event.

    ./capture_waybill.py                    # asks country, vendor, tracking ID
    ./capture_waybill.py --country PH --vendor JNT --tid EL4PL... [--no-print]

Flow: prompts -> duplicate check -> browser (persistent profile, manual sign-in if needed) -> select vendor
and scan the tracking ID in the real UI -> capture this scan's PRINTER:PRINT event -> populate the captured
template with the captured job (the application's own label.js) -> validate -> render through Labelary ->
save output/<COUNTRY>_<VENDOR>_<TID>/  See README.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Run with this tool's own .venv, whichever python started the script (./capture_waybill.py, python3 ...).
_VENV_PYTHON = Path(__file__).resolve().parent / ".venv" / "bin" / "python"
if _VENV_PYTHON.exists() and Path(sys.prefix).resolve() != _VENV_PYTHON.parent.parent.resolve():
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

import argparse  # noqa: E402
import asyncio  # noqa: E402
import contextlib  # noqa: E402
import time  # noqa: E402

from playwright.async_api import BrowserContext, Error as PlaywrightError, Frame, async_playwright  # noqa: E402

import artifacts  # noqa: E402
import browser  # noqa: E402
import labelary  # noqa: E402
import printer_check  # noqa: E402
import prompts  # noqa: E402
import report  # noqa: E402
from field_checks import check_fields  # noqa: E402
from errors import Failure, RunError  # noqa: E402
from ws_capture import PrintEvent, PrinterSocketMonitor, job_tracking_ids  # noqa: E402
from zpl_extract import DEFAULT_AGENT_LABEL_JS, PopulatedZpl, populate, preview_with_blank_fields  # noqa: E402

HERE = Path(__file__).resolve().parent
SOCKET_PRINTER_VERSION = 5  # Operator V2 "Socket Printer"; versions 1-4 are HTTP boxes


def step(n: int, text: str) -> None:
    report.get().line("step", f"[{n}/5] {text}")


def info(text: str) -> None:
    report.get().line("info", text)


def ok(text: str) -> None:
    report.get().line("ok", text)


def warn(text: str) -> None:
    report.get().line("warn", text)


def fail(text: str) -> None:
    report.get().line("fail", text)


def stage(name: str, state: str, detail: str = "") -> None:
    """Workflow stage for the web UI (report.STAGES); the terminal shows the lines above instead."""
    report.get().stage(name, state, detail)


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(HERE))
    except ValueError:
        return str(path)


# ----------------------------------------------------------------------------- workflow


async def prepare_tool(context: BrowserContext, inputs: prompts.RunInputs, args: argparse.Namespace):
    step(2, f"Opening the Cross Border Relabel Tool ({inputs.country})")
    stage("browser", "running", "Opening the Cross Border Relabel Tool in the authenticated browser")
    page = await browser.open_relabel_tool(
        context, inputs.country, args.login_timeout, args.profile / "nv-session-cookies.json"
    )
    info(page.url)

    step(3, "Checking the print-event socket")
    printer = None
    deadline = time.monotonic() + args.socket_timeout
    while time.monotonic() < deadline:
        printer = await browser.read_printer_config(page)
        if printer and (args.monitor.state.connected or printer.get("version") != SOCKET_PRINTER_VERSION):
            break
        await asyncio.sleep(0.5)
    if not printer:
        raise RunError(
            Failure.WEBSOCKET_FAILURE,
            "Operator V2 has no printer selected, so it never opens the printer socket",
            "select a Socket Printer in Operator V2 (Printer settings) and run again",
        )
    if printer.get("version") != SOCKET_PRINTER_VERSION:
        raise RunError(
            Failure.PRINTER_UNSUPPORTED,
            f"printer '{printer.get('name')}' is version {printer.get('version')} (HTTP printer box): it builds "
            "the ZPL inside the box, so no PRINTER:PRINT event exists to capture",
            "select a Socket Printer (version 5) in Operator V2 Printer settings",
        )
    state = args.monitor.state
    if not state.connected:
        detail = state.connect_error or state.socket_error or "no /printers namespace connection"
        raise RunError(
            Failure.WEBSOCKET_FAILURE,
            f"the printer WebSocket did not connect within {args.socket_timeout:.0f}s ({detail})",
            "reload Operator V2 or check the printer settings, then run again",
        )
    info(f"printer '{printer.get('name')}' (Socket Printer) - connected to {', '.join(state.urls)} /printers")
    stage("browser", "running", "Verifying the active printer on Operator V2 - Printers")
    summary = printer_check.verify(
        inputs.country,
        printer_check.local_printer_room(),
        await printer_check.read_printers_page(page.context, inputs.country),
        printer,
    )
    ok(f"Active printer verified: {summary}")
    stage("browser", "ok", f"Signed in to Operator V2; active printer verified: {summary}")

    step(4, f"Selecting vendor {inputs.vendor}")
    stage("submit", "running", f"Selecting vendor {inputs.vendor} and submitting {inputs.tracking_id}")
    await browser.wait_for_blocking_dialog(page, args.login_timeout)
    frame = await browser.relabel_frame(page)
    await browser.select_vendor(frame, inputs.vendor)
    return page, frame


async def scan_and_capture(frame: Frame, inputs: prompts.RunInputs, args: argparse.Namespace) -> PrintEvent:
    step(5, f"Scanning {inputs.tracking_id} and waiting for its PRINTER:PRINT event (timeout {args.timeout:.0f}s)")
    monitor: PrinterSocketMonitor = args.monitor
    await browser.watch_tool_messages(frame)
    lookup = args.lookups.expect(inputs.tracking_id)
    event_future = monitor.expect(inputs.tracking_id)
    await browser.scan_tracking_id(frame, inputs.tracking_id)
    stage("submit", "ok", f"{inputs.tracking_id} submitted through the relabel tool")
    stage("scan", "running", "Waiting for Operator V2 to accept the tracking ID")

    deadline = time.monotonic() + args.timeout
    error_seen_at: float | None = None
    scan_accepted = False
    while not event_future.done():
        messages = [m["text"] for m in await browser.tool_messages(frame) if m.get("error", True)]
        shown = f'Operator V2 says: "{" | ".join(messages)}"' if messages else None

        if lookup.done.is_set():
            if lookup.network_error:
                raise RunError(Failure.OPERATOR_ERROR, f"the tracking ID lookup failed: {lookup.network_error}")
            if lookup.status == 404:
                messages = messages or await _wait_for_messages(frame)
                raise RunError(Failure.INVALID_TRACKING_ID, _describe(inputs, messages, lookup))
            if lookup.status and lookup.status >= 400:
                messages = messages or await _wait_for_messages(frame)
                raise RunError(Failure.OPERATOR_ERROR, _describe(inputs, messages, lookup))
            if lookup.empty:
                raise RunError(
                    Failure.INVALID_TRACKING_ID,
                    f"Operator V2 has no cross-border relabel mapping for {inputs.tracking_id}; the tool prints nothing",
                )
            if lookup.vendor and lookup.vendor != inputs.vendor:
                messages = messages or await _wait_for_messages(frame)
                raise RunError(
                    Failure.VENDOR_MISMATCH,
                    f"{inputs.tracking_id} is a {lookup.vendor} parcel, not {inputs.vendor}. "
                    + (f'Operator V2 says: "{" | ".join(messages)}"' if messages else ""),
                    f"run again with vendor {lookup.vendor}",
                )
            if not scan_accepted and lookup.status and lookup.status < 400:
                scan_accepted = True
                stage("scan", "ok", f"Operator V2 accepted {inputs.tracking_id} (external {lookup.external_tracking_id})")
                stage("event", "running", "Waiting for the matching PRINTER:PRINT event")
        if shown:
            # An error toast without a failed mapping lookup (e.g. the order lookup failed). Give a late
            # print event a moment, then report what the tool said.
            error_seen_at = error_seen_at or time.monotonic()
            if time.monotonic() - error_seen_at > 2:
                failure = Failure.INVALID_TRACKING_ID if "not found" in shown.lower() else Failure.OPERATOR_ERROR
                raise RunError(failure, shown)
        if monitor.state.closed or monitor.state.connect_error or monitor.state.socket_error:
            detail = monitor.state.connect_error or monitor.state.socket_error or "socket closed"
            raise RunError(Failure.WEBSOCKET_FAILURE, f"the printer WebSocket failed during the scan ({detail})")
        if time.monotonic() > deadline:
            if lookup.done.is_set():
                raise RunError(
                    Failure.MISSING_PRINT_EVENT,
                    f"Operator V2 accepted {inputs.tracking_id} (external {lookup.external_tracking_id}) but sent no "
                    f"PRINTER:PRINT event within {args.timeout:.0f}s",
                    "check the tool's print log in the browser; a 'Success' there without an event means the job "
                    "was not sent over the printer socket",
                )
            raise RunError(
                Failure.TIMEOUT,
                f"no response to the scan within {args.timeout:.0f}s (no lookup result, no print event)",
                "check the browser window, then run again (raise --timeout on a slow network)",
            )
        await asyncio.sleep(0.25)
    if not scan_accepted:  # the print event arrived before the lookup response was read
        stage("scan", "ok", f"Operator V2 accepted {inputs.tracking_id}")
    return event_future.result()


async def _wait_for_messages(frame: Frame, seconds: float = 3) -> list[str]:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        messages = [m["text"] for m in await browser.tool_messages(frame) if m.get("error", True)]
        if messages:
            return messages
        await asyncio.sleep(0.2)
    return []


def _describe(inputs: prompts.RunInputs, messages: list[str], lookup: browser.Lookup) -> str:
    parts = [f"{inputs.tracking_id} was rejected by Operator V2."]
    if messages:
        parts.append(f'Operator V2 says: "{" | ".join(messages)}".')
    if lookup.status:
        parts.append(f"API: HTTP {lookup.status}" + (f" - {lookup.api_message}" if lookup.api_message else "") + ".")
    return " ".join(parts)


async def agent_status(monitor: PrinterSocketMonitor, event: PrintEvent, seconds: float) -> str:
    if not event.forwarded_to_printer:
        return "not sent to the printer (--no-print)"
    _, ext = job_tracking_ids(event.job)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        statuses = monitor.job_updates.get(ext or "", [])
        if any(s.startswith("PRINT:UPDATE_JOB") or "FAIL" in s for s in statuses):
            break
        await asyncio.sleep(0.25)
    statuses = monitor.job_updates.get(ext or "", [])
    return " -> ".join(statuses) if statuses else "no status from the printer agent"




# ----------------------------------------------------------------------------- waybill


def generate_waybill(
    store: artifacts.OutputStore,
    inputs: prompts.RunInputs,
    plan: prompts.DuplicatePlan,
    event: PrintEvent,
    status: str,
    args: argparse.Namespace,
) -> int:
    """Everything after the print event: populate, validate, render, verify, then commit to the folder."""
    nv, ext = job_tracking_ids(event.job)
    template_name = event.job.get("template")
    report.get().line("plain", "")
    ok(f"PRINTER:PRINT event captured ({event.socket_url} {event.namespace})")
    ok(f"Matching job found (nv_tracking_id={nv}, tracking_id={ext}, template={template_name})")
    stage("event", "ok", f"PRINTER:PRINT captured; job nv_tracking_id={nv}, tracking_id={ext}")
    report.get().result(tracking_id=inputs.tracking_id, external_tracking_id=ext, template=template_name,
                        printer_status=status)
    staged = store.stage(inputs)

    def give_up(label: str, error: RunError) -> int:
        fail(f"{label}: {error.message}")
        if error.hint:
            info(f"-> {error.hint}")
        stage("*", "failed", f"{error.message}" + (f" -> {error.hint}" if error.hint else ""))
        if args.blank_missing_fields and error.failure is Failure.UNRESOLVED_PLACEHOLDERS:
            for kind, path in blank_fields_preview(store, staged, inputs, event, args).items():
                info(f"  {kind:<12}: {rel(path)}")
        store.discard(staged)
        report_not_generated(store, inputs, plan)
        info(f"Printer agent: {status}")
        report.get().result(error=error.message, failure=error.failure.name, exit_code=error.failure.exit_code)
        return error.failure.exit_code

    stage("generate", "running", "Populating the captured template with the captured job")
    try:
        zpl = populate(event.job, args.agent_label_js, args.allow_python_renderer)
    except RunError as e:
        return give_up("Template populated", e)
    filled = len(zpl.placeholders)
    extra = f"; optional left empty: {', '.join(zpl.optional_blanked)}" if zpl.optional_blanked else ""
    renderer = ("the printer app's label.js" if (zpl.engine or "").startswith("nv-label-printer")
                else "the verified Python copy of label.js")
    ok(f"Template populated ({filled} placeholders with {renderer}{extra})" if filled else f"ZPL taken from the event ({zpl.origin})")
    stage("generate", "ok", f"{filled} placeholders populated with {renderer}{extra}")
    report.get().result(zpl=zpl.text)  # shown even if rendering fails later
    ok("No unresolved placeholders")
    stage("validate", "ok", "No unresolved placeholders (field checks follow after rendering)")
    staged.write("zpl", zpl.data)

    stage("render", "running", "Rendering the waybill through Labelary")
    geometry = labelary.label_geometry(zpl.data, template_name, args.dpmm)
    lint: list[dict] = []
    for fmt in args.formats:
        try:
            rendered = labelary.render(zpl.data, staged.path(fmt), fmt, geometry)
        except RunError as e:
            return give_up(f"Waybill {fmt.upper()} rendered", e)
        staged.files[fmt] = rendered.path
        lint = lint or rendered.lint
    stage("render", "ok", f"{', '.join(f.upper() for f in args.formats)} rendered at {geometry.width_in}x{geometry.height_in} in, "
                          f"{geometry.dpmm} dpmm")

    field_results = check_fields(zpl.template, zpl.text, event.job, lint)
    for check in field_results:
        {"ok": ok, "warn": warn, "failed": fail}[check.status](f"{check.field}: {check.detail}")
    checks, problems = artifacts.verify_staged(staged, inputs, event.job, zpl.text, plan.replaces)
    problems += [f"{c.field}: {c.detail}" for c in field_results if c.status == "failed"]
    report.get().result(field_checks=[c.__dict__ for c in field_results])
    if problems:
        stage("render", "ok")
        return give_up("Waybill verified", RunError(Failure.VERIFICATION, "; ".join(problems)))
    field_warnings = [f"{c.field}: {c.detail}" for c in field_results if c.status == "warn"]
    stage("validate", "warn" if field_warnings else "ok",
          "; ".join(field_warnings) if field_warnings else "No unresolved placeholders; remarks, sorting codes, goods and pieces checked")

    stage("save", "running", "Saving the waybill files")
    result = store.commit(staged, inputs, plan.replaces)
    post = artifacts.verify_committed(store, inputs, result, plan.replaces)
    if post:
        fail("Saved waybill verified: " + "; ".join(post))
        stage("save", "failed", "; ".join(post))
        report.get().result(error="; ".join(post), failure=Failure.VERIFICATION.name, exit_code=Failure.VERIFICATION.exit_code)
        return Failure.VERIFICATION.exit_code
    ok(f"Duplicate tracking ID check completed ({duplicate_summary(plan, result)})")
    if plan.replaces:
        ok(
            f"Replacement verified: {plan.replaces} no longer appears in the folder or file names; "
            f"{inputs.tracking_id}'s label carries {', '.join(checks['tracking_id_printed_on_label'])}"
        )

    ok(f"Folder: {rel(result.folder)}/")
    ok(f"ZPL saved: {rel(result.saved['zpl'])}")
    if "png" in result.saved:
        ok(f"Waybill image saved: {rel(result.saved['png'])}")
    if "pdf" in result.saved:
        ok(f"Waybill PDF saved: {rel(result.saved['pdf'])}")
    info(f"Label {geometry.width_in}x{geometry.height_in} in at {geometry.dpmm} dpmm ({geometry.source})")
    info(f"Printer agent: {status}")
    for warning in zpl.warnings:
        warn(warning)
    if lint:
        warn(f"Labelary lint ({len(lint)}): " + "; ".join(labelary.format_lint(w) for w in lint[:3]) + (" ..." if len(lint) > 3 else ""))
    if args.monitor.duplicates:
        warn(f"the page sent {args.monitor.duplicates} extra PRINTER:PRINT event(s) for this scan (duplicate prints)")
    stage("save", "ok", f"{rel(result.folder)}/ - {duplicate_summary(plan, result)}")
    report.get().result(
        exit_code=0,
        folder=str(result.folder),
        files={kind: str(path) for kind, path in result.saved.items()},
        duplicate=duplicate_summary(plan, result),
        label={"width_in": geometry.width_in, "height_in": geometry.height_in, "dpmm": geometry.dpmm},
        warnings=zpl.warnings + [labelary.format_lint(w) for w in lint],
    )
    return 0


def duplicate_summary(plan: prompts.DuplicatePlan, result: artifacts.CommitResult) -> str:
    if plan.replaces:
        how = f"folder renamed from {result.renamed_from.name}" if result.renamed_from else "new folder"
        return f"{plan.replaces} replaced by {plan.inputs.tracking_id}; {how}, {len(result.removed)} old file(s) removed"
    if plan.summary != "no duplicate":
        return f"{plan.inputs.tracking_id} scanned again; folder kept, {len(result.removed)} old file(s) replaced"
    return "no duplicate"


def blank_fields_preview(
    store: artifacts.OutputStore, staged: artifacts.Staged, inputs: prompts.RunInputs, event: PrintEvent,
    args: argparse.Namespace,
) -> dict:
    """--blank-missing-fields: a marked preview with the unresolved fields left blank."""
    try:
        zpl: PopulatedZpl = preview_with_blank_fields(event.job, args.agent_label_js, args.allow_python_renderer)
    except RunError as e:
        warn(f"No preview: {e.message}")
        return {}
    staged.write("preview_zpl", zpl.data)
    geometry = labelary.label_geometry(zpl.data, event.job.get("template"), args.dpmm)
    for fmt in args.formats:
        try:
            staged.files[f"preview_{fmt}"] = labelary.render(zpl.data, staged.path(f"preview_{fmt}"), fmt, geometry).path
        except RunError as e:
            warn(f"Preview {fmt.upper()} failed: {e.message}")
    report.get().line("plain", f"[PREVIEW - NOT THE PRINTED LABEL] {', '.join(zpl.preview_blanked)} left blank (--blank-missing-fields)")
    return store.save_preview(staged, inputs)


def report_not_generated(store: artifacts.OutputStore, inputs: prompts.RunInputs, plan: prompts.DuplicatePlan) -> None:
    info("No waybill was generated.")
    if plan.replaces:
        info(f"Replacement NOT done: {plan.replaces}'s waybill is unchanged.")
    elif plan.summary != "no duplicate":
        info(f"The previous waybill of {inputs.tracking_id} is unchanged.")


def report_failure(error: RunError, store=None, inputs=None, plan=None) -> int:
    report.get().line("plain", "")
    fail(f"{error.failure.label}: {error.message}")
    if error.hint:
        info(f"-> {error.hint}")
    stage("*", "failed", f"{error.failure.label}: {error.message}" + (f" -> {error.hint}" if error.hint else ""))
    report.get().result(error=error.message, hint=error.hint, failure=error.failure.name, exit_code=error.failure.exit_code)
    if store and inputs and plan and error.failure not in (Failure.INPUT, Failure.AUTH, Failure.DUPLICATE):
        report_not_generated(store, inputs, plan)
    return error.failure.exit_code


# ----------------------------------------------------------------------------- main


def plan_duplicates(store: artifacts.OutputStore, inputs: prompts.RunInputs, args: argparse.Namespace) -> prompts.DuplicatePlan:
    found = store.existing(inputs)
    existing = []
    if found["folder"]:
        names = ", ".join(p.name for p in found["files"]) or "empty"
        existing.append(f"{rel(found['folder'])}/ ({names})")
    existing += [f"{rel(p)} (earlier script version)" for p in found["stray"]]
    return prompts.resolve_duplicate(
        inputs, existing, lambda tid: store.is_taken(inputs.country, inputs.vendor, tid),
        args.replace_with, args.force, args.no_print,
    )


async def run(args: argparse.Namespace) -> int:
    inputs = prompts.collect_inputs(args.country, args.vendor, args.tid)
    print(f"\nCountry: {inputs.country}\nVendor: {inputs.vendor}\nTracking ID: {inputs.tracking_id}")
    store = artifacts.OutputStore(args.out)
    plan = plan_duplicates(store, inputs, args)
    inputs = plan.inputs
    if plan.replaces:
        print(f"\nTracking ID: {inputs.tracking_id} (replaces {plan.replaces})")

    return await execute(store, inputs, plan, args)


async def execute(store: artifacts.OutputStore, inputs: prompts.RunInputs, plan: prompts.DuplicatePlan,
                  args: argparse.Namespace) -> int:
    """Browser, scan, capture and waybill for already validated inputs (shared by the CLI and the agent)."""
    step(1, "Starting the browser" + (" (--no-print: print jobs are captured, not sent to the printer)" if args.no_print else ""))
    stage("browser", "running", "Starting the authenticated browser (persistent Operator V2 profile)")
    async with async_playwright() as pw:
        try:
            context = await browser.launch_context(pw, args.profile, args.channel)
        except PlaywrightError as e:
            return report_failure(RunError(Failure.BROWSER, f"could not start the browser: {str(e).splitlines()[0]}"))
        args.monitor = PrinterSocketMonitor(no_print=args.no_print)
        args.lookups = browser.LookupWatcher()
        try:
            await args.monitor.attach(context)
            context.on("page", args.lookups.attach)
            for page in context.pages:
                args.lookups.attach(page)
            page, frame = await prepare_tool(context, inputs, args)
            event = await scan_and_capture(frame, inputs, args)
            status = await agent_status(args.monitor, event, args.status_wait)
            return generate_waybill(store, inputs, plan, event, status, args)
        except RunError as e:
            return report_failure(e, store, inputs, plan)
        except PlaywrightError as e:
            return report_failure(RunError(Failure.BROWSER, str(e).splitlines()[0]), store, inputs, plan)
        finally:
            # After a terminal Ctrl+C the Playwright driver got SIGINT too and may already be gone.
            with contextlib.suppress(Exception):
                await context.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--country", type=str.upper, choices=sorted(prompts.COUNTRY_VENDORS), help="skip the country prompt")
    p.add_argument("--vendor", type=str.upper, help="skip the vendor prompt (PH: JNT/FLASH, MY: JNT)")
    p.add_argument("--tid", help="skip the tracking ID prompt")
    p.add_argument("--replace-with", metavar="NEW_TID", help="if --tid already has a waybill, replace it with this tracking ID")
    p.add_argument("--force", action="store_true", help="if the tracking ID already has a waybill, generate it again")
    p.add_argument("--no-print", action="store_true", help="capture the print job but do not send it to the printer")
    p.add_argument("--timeout", type=float, default=45, help="seconds to wait for the PRINTER:PRINT event (default 45)")
    p.add_argument("--socket-timeout", type=float, default=30, help="seconds to wait for the printer socket (default 30)")
    p.add_argument("--status-wait", type=float, default=20, help="seconds to wait for the printer agent's status")
    p.add_argument("--login-timeout", type=float, default=600, help="seconds allowed for a manual sign-in")
    p.add_argument("--out", type=Path, default=HERE / "output", help="output folder (default ./output)")
    p.add_argument("--format", dest="formats", choices=["png", "pdf", "both"], default="both")
    p.add_argument("--dpmm", type=int, choices=[6, 8, 12, 24], help="override the resolution derived from the template")
    p.add_argument("--profile", type=Path, default=HERE / ".browser-profile", help="persistent browser profile")
    p.add_argument("--channel", default="chrome", help="Chromium channel; '' = Playwright's bundled Chromium")
    p.add_argument("--agent-label-js", type=Path, default=DEFAULT_AGENT_LABEL_JS, help="the agent's label.js")
    p.add_argument("--allow-python-renderer", action="store_true", help="use the Python port if the agent is missing")
    p.add_argument(
        "--blank-missing-fields",
        action="store_true",
        help="if required template fields are missing, also render a marked PREVIEW with them blank (debug only)",
    )
    args = p.parse_args(argv)
    args.formats = ["png", "pdf"] if args.formats == "both" else [args.formats]
    args.profile = args.profile.resolve()
    args.out = args.out.resolve()
    return args


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except RunError as e:  # input / duplicate handling before the browser starts
        return report_failure(e)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
