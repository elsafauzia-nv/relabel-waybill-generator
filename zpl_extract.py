"""Populate the captured template into the final ZPL, using the application's own conversion logic.

What the payload holds (verified on QA): Operator V2 does not send finished ZPL for relabel prints. Each
``PRINTER:PRINT`` job carries ``template_string`` (``{{field[filters]}}`` placeholders) plus the field values;
the nv-label-printer agent populates it with server/helpers/label.js#generateZpl and prints the result.

This module runs that same label.js (installed agent, via render_with_agent.js) on the captured job, so every
placeholder and filter - ``uppercase``, ``limit:N``, ``convert_to_zpl_hex``, ``font``, the agent's built-ins -
behaves exactly as in the application. Nothing outside the placeholders is touched. On top of it:

* required placeholders the job does not send are reported and stop the run (the agent would print the
  literal text "undefined", or crash on a filter); nothing is guessed;
* optional placeholders - those the relabel tool sends only conditionally - get their intended empty value;
* the final text is validated (no ``{{...}}`` left, no "undefined"/"[object Object]" introduced);
* text is encoded the way the template declares (``^CI28`` = UTF-8), so Chinese/Thai text is kept intact.
  The agent itself writes each character truncated to 8 bits; where that would corrupt text it is reported.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_label_port import AgentLabelPort, AgentRenderError, _utf16_units
from errors import Failure, RunError

HERE = Path(__file__).resolve().parent
DEFAULT_AGENT_LABEL_JS = Path(
    "/Applications/nv-label-printer-ason.app/Contents/Resources/app/server/helpers/label.js"
)
_PLACEHOLDER = re.compile(r"{{([^{]+)}}")
_LEFTOVER = re.compile(r"{{[^}]*}}")
_UTF8_MODE = re.compile(r"\^CI\s*28")

# Placeholders the agent fills itself (label.js#generateZpl).
AGENT_BUILTINS = {"printer_x_offset", "printer_y_offset", "tracking_id", "font", "logo", "quantity"}

# Fields the relabel tool sends only conditionally (cross-border-relabel-tool page bundle). When absent the
# intended value is empty: the agent would write the literal word "undefined" instead.
OPTIONAL_FIELDS = {
    "cod_watermark_zpl": "sent only for COD parcels (`c && {cod_watermark_zpl: c}`, c = watermark when cod > 0)",
}


@dataclass
class PopulatedZpl:
    text: str  # final ZPL
    data: bytes  # what is saved / rendered (encoded as the template declares)
    encoding: str
    origin: str
    engine: str | None
    template: str | None
    placeholders: dict[str, str] = field(default_factory=dict)  # field -> placeholder expression
    optional_blanked: list[str] = field(default_factory=list)
    empty_values: list[str] = field(default_factory=list)
    preview_blanked: list[str] = field(default_factory=list)  # only for --blank-missing-fields previews
    warnings: list[str] = field(default_factory=list)


def find_zpl_fields(obj: Any, path: str = "job") -> list[tuple[str, str]]:
    """(key path, "zpl" | "template") for every string in the job that contains a ZPL label."""
    found: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            found += find_zpl_fields(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found += find_zpl_fields(value, f"{path}[{i}]")
    elif isinstance(obj, str) and "^XA" in obj:
        found.append((path, "template" if _PLACEHOLDER.search(obj) else "zpl"))
    return found


def placeholder_fields(template: str) -> dict[str, str]:
    """Field name -> placeholder expression using it (one with filters preferred, e.g. "barangay[uppercase]")."""
    names: dict[str, str] = {}
    for expression in _PLACEHOLDER.findall(template):
        name = re.sub(r"(.+)\[.*\]*", r"\1", expression, count=1)
        if name not in names or "[" in expression:
            names[name] = expression
    return names


def missing_fields(template: str, job: dict[str, Any]) -> dict[str, str]:
    """Template fields the job does not send, or sends as null (the agent fills its built-ins itself)."""
    return {n: e for n, e in placeholder_fields(template).items() if job.get(n) is None and n not in AGENT_BUILTINS}


def printer_bytes(zpl: str) -> bytes:
    """What the agent's printJob writes: each UTF-16 code unit into a Uint8Array (low 8 bits)."""
    return bytes(unit & 0xFF for unit in _utf16_units(zpl))


def populate(
    job: dict[str, Any],
    agent_label_js: Path = DEFAULT_AGENT_LABEL_JS,
    allow_python_renderer: bool = False,
    blank_required: bool = False,
) -> PopulatedZpl:
    """Final ZPL for a captured job. ``blank_required`` is only for the marked --blank-missing-fields preview."""
    template = job.get("template_string") if isinstance(job.get("template_string"), str) else None

    if job.get("zpl"):  # the application sent finished ZPL: use it as is
        text = str(job["zpl"])
        result = _finish(text, PopulatedZpl(text, b"", "", "payload field job.zpl, unmodified", None, template))
        return result

    if job.get("system_id") is not None and _positive(job.get("template_id")):
        raise RunError(Failure.ZPL_EXTRACTION, "the job references an agent-local template_id; its template is not in the payload")
    if template is None or job.get("system_id") is None:
        found = ", ".join(f"{p} ({k})" for p, k in find_zpl_fields(job)) or "none"
        raise RunError(Failure.ZPL_EXTRACTION, f"the print event has no ZPL and no template_string (ZPL-like fields: {found})")

    placeholders = placeholder_fields(template)
    wrong_type = [n for n in placeholders if isinstance(job.get(n), (dict, list))]
    missing = missing_fields(template, job)
    optional = [n for n in missing if n in OPTIONAL_FIELDS]
    required = [n for n in missing if n not in OPTIONAL_FIELDS]
    if (required or wrong_type) and not blank_required:
        raise _unresolved(required, wrong_type, placeholders)

    patched = {**job, **{n: "" for n in optional}}
    if blank_required:
        patched.update({n: "" for n in required + wrong_type})
    text, engine = _run_label_js(patched, agent_label_js, allow_python_renderer)

    result = PopulatedZpl(
        text, b"", "", "captured template_string populated with the captured job's fields", engine, template,
        placeholders=placeholders,
        optional_blanked=optional,
        empty_values=[n for n in placeholders if job.get(n) == "" and n not in AGENT_BUILTINS],
        preview_blanked=(required + wrong_type) if blank_required else [],
    )
    for name in optional:
        result.warnings.append(f"{name} not in the job ({OPTIONAL_FIELDS[name]}); left empty as intended")
    result.warnings += _template_notes(template, job)
    return _finish(text, result, template)


def _unresolved(required: list[str], wrong_type: list[str], placeholders: dict[str, str]) -> RunError:
    parts = []
    if required:
        parts.append("the print event does not include " + ", ".join(f"{{{{{placeholders[n]}}}}}" for n in required))
    if wrong_type:
        parts.append("these fields are objects/lists, not text: " + ", ".join(wrong_type))
    crash = [n for n in required if "[" in placeholders[n]]
    effect = (
        "the printer agent fails this job (its filters cannot run on a missing value)"
        if crash
        else "the printer agent would print the word 'undefined' there"
    )
    return RunError(
        Failure.UNRESOLVED_PLACEHOLDERS,
        f"unresolved required placeholders: {'; '.join(parts)} - {effect}",
        "the print event and the printer template do not match (Operator V2 / template issue, not a capture "
        "problem); --blank-missing-fields renders a marked preview",
    )


def _finish(text: str, result: PopulatedZpl, template: str | None = None) -> PopulatedZpl:
    """Validate the populated text and encode it as the template declares."""
    leftover = sorted(set(_LEFTOVER.findall(text)))
    if leftover:
        raise RunError(Failure.UNRESOLVED_PLACEHOLDERS, f"placeholders left in the final ZPL: {', '.join(leftover)}")
    for token in ("undefined", "[object Object]"):
        if text.count(token) > (template or "").count(token):
            raise RunError(Failure.UNRESOLVED_PLACEHOLDERS, f"the populated ZPL contains '{token}' from an unresolved value")
    if not (text.lstrip().startswith("^XA") and text.rstrip().endswith("^XZ")):
        raise RunError(Failure.ZPL_EXTRACTION, "the populated ZPL does not start with ^XA and end with ^XZ")

    if _UTF8_MODE.search(text):
        result.data, result.encoding = text.encode("utf-8"), "utf-8 (template declares ^CI28)"
    else:
        try:
            result.data, result.encoding = text.encode("latin-1"), "latin-1 (template has no ^CI28)"
        except UnicodeEncodeError:
            bad = sorted({c for c in text if ord(c) > 0xFF})
            raise RunError(
                Failure.ZPL_EXTRACTION,
                f"the template does not declare UTF-8 (^CI28) and the text contains {''.join(bad[:20])!r}, which "
                "cannot be encoded without losing characters",
            ) from None
    return result


def _template_notes(template: str, job: dict[str, Any]) -> list[str]:
    notes = []
    no_fh = []
    for block in template.split("^FS"):
        if "convert_to_zpl_hex" in block and "^FH" not in block:
            no_fh += [re.sub(r"\[.*", "", e) for e in _PLACEHOLDER.findall(block) if "convert_to_zpl_hex" in e]
    if no_fh:
        notes.append(
            "template bug: " + ", ".join(dict.fromkeys(no_fh)) + " are hex-encoded (convert_to_zpl_hex) but their "
            "fields have no ^FH, so they print as literal _xx hex (on the real label too)"
        )
    non_ascii = [
        n for n, e in placeholder_fields(template).items()
        if "convert_to_zpl_hex" not in e and isinstance(job.get(n), str) and any(ord(c) > 0x7F for c in job[n])
    ]
    if non_ascii:
        notes.append(
            "non-Latin text in " + ", ".join(non_ascii) + ": kept intact (UTF-8, ^CI28) in the saved ZPL and waybill, "
            "but the printer agent cuts every character to 8 bits, so the physical label shows garbage there "
            "(agent bug)"
        )
    return notes


def _positive(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _run_label_js(job: dict[str, Any], agent_label_js: Path, allow_python_renderer: bool) -> tuple[str, str]:
    node = shutil.which("node")
    if agent_label_js.is_file() and node:
        proc = subprocess.run(
            [node, str(HERE / "render_with_agent.js"), str(agent_label_js)],
            input=json.dumps(job),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if proc.returncode != 0:
            raise RunError(Failure.ZPL_EXTRACTION, proc.stderr.strip() or f"agent renderer exited {proc.returncode}")
        return json.loads(proc.stdout)["zpl"], f"nv-label-printer agent label.js ({agent_label_js})"

    if not allow_python_renderer:
        missing = "node" if not node else f"the nv-label-printer agent ({agent_label_js})"
        raise RunError(
            Failure.ZPL_EXTRACTION,
            f"cannot populate the template: {missing} not found",
            "install the agent / node, point --agent-label-js at label.js, or pass --allow-python-renderer",
        )
    try:
        return AgentLabelPort.generate_zpl(job["template_string"], job), "Python port of the agent's label.js"
    except AgentRenderError as e:
        raise RunError(Failure.ZPL_EXTRACTION, str(e)) from None


# ----------------------------------------------------------------------------- opt-in preview

# Black bar with white (reversed) text across the label, so the image cannot pass for the printed label.
PREVIEW_BANNER = "^FO20,560^GB760,80,80^FS^FO40,582^A0N,40,40^FR^FDPREVIEW - NOT THE PRINTED LABEL^FS"


def preview_with_blank_fields(
    job: dict[str, Any], agent_label_js: Path = DEFAULT_AGENT_LABEL_JS, allow_python_renderer: bool = False
) -> PopulatedZpl:
    """--blank-missing-fields: populate with unresolved required fields set to "" and stamp a banner.

    This is NOT what the printer printed (the agent fails the real job); it only shows the rest of the label.
    """
    result = populate(job, agent_label_js, allow_python_renderer, blank_required=True)
    if not result.preview_blanked:
        raise RunError(Failure.ZPL_EXTRACTION, "nothing to blank; a preview would not help")
    end = result.text.rfind("^XZ")
    result.text = result.text[:end] + PREVIEW_BANNER + "\n" + result.text[end:]
    result.data = result.text.encode("utf-8" if result.encoding.startswith("utf-8") else "latin-1", "replace")
    result.origin = f"PREVIEW ONLY: {', '.join(result.preview_blanked)} left blank, plus a preview banner"
    return result
