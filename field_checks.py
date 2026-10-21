"""Checks of the fields whose rules come from Operator V2's Cross Border Relabel Tool.

The rules are the tool's own (cross-border-relabel-tool page bundle, verified 2026-10-21); this script does
not change any value, it checks that the populated ZPL carries them intact and that they fit their box:

* remarks:  ``k(remark)`` keeps up to 250 characters; longer becomes 247 characters + "...";
* fourth_sorting_code: ``T(code, 25).toUpperCase()`` - at most 25 characters, uppercase, on its own line;
* goods / pieces: goods = item names joined with ", ", pieces = number of items (1 when there are none).

Whether a value fits its box in the template is taken from Labelary's lint for that field
("Text does not fit on N lines" etc.): a too-small box is a template issue and is reported as a warning,
because the rendered waybill then shows exactly what the printer would print.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REMARKS_MAX = 250
FOURTH_SORTING_CODE_MAX = 25
_PLACEHOLDER = re.compile(r"{{([^{]+)}}")


@dataclass
class FieldCheck:
    field: str
    status: str  # ok | warn | failed
    detail: str


def _field_span(template: str, zpl_text: str, name: str) -> tuple[int, int] | None:
    """Byte span (in the UTF-8 ZPL) of the ^FD...^FS field that holds placeholder ``name``."""
    for block in template.split("^FS"):
        m = re.search(r"{{" + re.escape(name) + r"(\[[^\]]*\])?}}", block)
        if not m or "^FD" not in block[: m.start()]:
            continue
        prefix = block[: m.start()].lstrip("\n")
        if _PLACEHOLDER.search(prefix):
            prefix = prefix[prefix.rfind("^F"):]  # keep the part right before the placeholder
        at = zpl_text.find(prefix)
        if at < 0:
            return None
        fd = at + prefix.rfind("^FD")
        fs = zpl_text.find("^FS", fd)
        return len(zpl_text[:fd].encode()), len(zpl_text[: fs if fs >= 0 else len(zpl_text)].encode())
    return None


def _lint_for(lint: list[dict], span: tuple[int, int] | None) -> tuple[list[str], list[str]]:
    """Labelary lint of one field, split into (does-not-fit messages, other messages - e.g. missing glyphs)."""
    if not span:
        return [], []
    messages = [w["message"] for w in lint if span[0] <= w["offset"] <= span[1]]
    return [m for m in messages if "fit" in m.lower()], [m for m in messages if "fit" not in m.lower()]


def _glyph_note(other: list[str]) -> str:
    return f" (font note: {'; '.join(other)} - the template's font has no glyphs for some characters)" if other else ""


def _expression(template: str, name: str) -> str | None:
    return next((e for e in _PLACEHOLDER.findall(template) if re.sub(r"\[.*", "", e) == name), None)


def check_fields(template: str | None, zpl_text: str, job: dict, lint: list[dict]) -> list[FieldCheck]:
    if not template:
        return []
    checks: list[FieldCheck] = []

    if _expression(template, "remarks") is not None:
        value = str(job.get("remarks") or "")
        n = len(value)
        fit, other = _lint_for(lint, _field_span(template, zpl_text, "remarks"))
        if n > REMARKS_MAX:
            checks.append(FieldCheck("remarks", "failed", f"{n} characters - over Operator V2's {REMARKS_MAX}-character limit"))
        elif value and value not in zpl_text and "[" not in _expression(template, "remarks"):
            checks.append(FieldCheck("remarks", "failed", "the remarks text is not complete in the ZPL"))
        elif fit:
            checks.append(FieldCheck("remarks", "warn", f"{n}/{REMARKS_MAX} characters, complete in the ZPL, but the "
                                     f"template's remarks box is too small: {'; '.join(fit)} (the printer cuts it too)"
                                     + _glyph_note(other)))
        elif other:
            checks.append(FieldCheck("remarks", "warn", f"{n}/{REMARKS_MAX} characters, complete and inside its box"
                                     + _glyph_note(other)))
        else:
            checks.append(FieldCheck("remarks", "ok", f"{n}/{REMARKS_MAX} characters, complete and inside its box"))

    expression = _expression(template, "fourth_sorting_code")
    if expression is not None:
        value = str(job.get("fourth_sorting_code") or "")
        span = _field_span(template, zpl_text, "fourth_sorting_code")
        block = next((b for b in template.split("^FS") if "{{" + expression + "}}" in b), "")
        multi_line = re.search(r"\^FB\d+,(\d+)", block)
        fit, other = _lint_for(lint, span)
        if not value:
            checks.append(FieldCheck("fourth_sorting_code", "ok", "empty - Operator V2 sent no fourth sorting code for this parcel"))
        elif len(value) > FOURTH_SORTING_CODE_MAX:
            checks.append(FieldCheck("fourth_sorting_code", "failed",
                                     f"{len(value)} characters - over Operator V2's {FOURTH_SORTING_CODE_MAX}-character limit"))
        elif value.upper() not in zpl_text:
            checks.append(FieldCheck("fourth_sorting_code", "failed", "the fourth sorting code is not in the ZPL"))
        elif multi_line and int(multi_line.group(1)) > 1:
            checks.append(FieldCheck("fourth_sorting_code", "warn", "its field may wrap over several lines (^FB)"))
        elif fit or other:
            checks.append(FieldCheck("fourth_sorting_code", "warn", f"does not fit its line: {'; '.join(fit + other)}"))
        else:
            checks.append(FieldCheck("fourth_sorting_code", "ok",
                                     f"'{value.upper()}' ({len(value)}/{FOURTH_SORTING_CODE_MAX}) on its own single-line field"))

    if _expression(template, "goods") is not None or _expression(template, "pieces") is not None:
        goods = str(job.get("goods") or "")
        pieces = job.get("pieces")
        fit, other = _lint_for(lint, _field_span(template, zpl_text, "goods"))
        if not isinstance(pieces, int) or pieces < 1:
            checks.append(FieldCheck("goods_pieces", "failed", f"pieces is {pieces!r}; the tool sends the item count (at least 1)"))
        elif goods and goods not in zpl_text and "[" not in (_expression(template, "goods") or ""):
            checks.append(FieldCheck("goods_pieces", "failed", "the goods text is not complete in the ZPL"))
        elif fit:
            checks.append(FieldCheck("goods_pieces", "warn", f"{pieces} piece(s); the goods box is too small: {'; '.join(fit)}"
                                     + _glyph_note(other)))
        elif other:
            checks.append(FieldCheck("goods_pieces", "warn", f"{pieces} piece(s); goods complete and inside its box"
                                     + _glyph_note(other)))
        else:
            checks.append(FieldCheck("goods_pieces", "ok", f"{pieces} piece(s); goods complete and inside its box"))
    return checks
