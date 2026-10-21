"""Line-for-line Python port of the nv-label-printer agent's server/helpers/label.js (generateZpl).

Only used with --allow-python-renderer when the agent is not installed. Verified byte-for-byte against
the installed agent, including its quirks (missing fields print "undefined", $-patterns in values are
expanded by String.replace, ZPL hex without zero padding, astral characters make the agent throw).
"""

from __future__ import annotations

import re
from typing import Any


class AgentRenderError(Exception):
    """The agent itself would fail to generate ZPL for this job."""


def _js_gt0(v: Any) -> bool:
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def _utf16_units(s: str) -> list[int]:
    raw = s.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]


def _from_utf16_units(units: list[int]) -> str:
    return b"".join(u.to_bytes(2, "little") for u in units).decode("utf-16-le", "surrogatepass")


_UNDEFINED = object()


def _js_str(v: Any) -> str:
    """JavaScript String(v) for JSON-shaped values."""
    if v is _UNDEFINED:
        return "undefined"
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, list):
        return ",".join("" if x is None else _js_str(x) for x in v)
    if isinstance(v, dict):
        return "[object Object]"
    return str(v)


def _js_truthy(v: Any) -> bool:
    return v is not _UNDEFINED and v not in (None, False, 0, "")


def _js_replace_first(haystack: str, needle: str, replacement: str) -> str:
    """String.prototype.replace(string, string) including $-pattern expansion."""
    i = haystack.find(needle)
    if i < 0:
        return haystack
    out, j = [], 0
    while j < len(replacement):
        c = replacement[j]
        if c == "$" and j + 1 < len(replacement):
            n = replacement[j + 1]
            if n == "$":
                out.append("$")
            elif n == "&":
                out.append(needle)
            elif n == "`":
                out.append(haystack[:i])
            elif n == "'":
                out.append(haystack[i + len(needle) :])
            else:
                out.append(c)
                j += 1
                continue
            j += 2
            continue
        out.append(c)
        j += 1
    return haystack[:i] + "".join(out) + haystack[i + len(needle) :]


class AgentLabelPort:
    """Line-for-line port of nv-label-printer server/helpers/label.js (generateZpl and helpers)."""

    @staticmethod
    def generate_zpl(template: str, job: dict[str, Any]) -> str:
        variables: dict[str, dict[str, Any]] = {}
        for variable in re.findall(r"{{[^{]+}}", template):
            expression = re.sub(r"{{([^{]+)}}", r"\1", variable, count=1)
            display_name = re.sub(r"(.+)\[.*\]*", r"\1", expression, count=1)
            ops_match = re.search(r"\[([^\]]*)\]", expression)
            operations = ops_match.group(1).split(",") if ops_match else None
            value: Any = ""
            if display_name == "printer_x_offset":
                value = 140
            elif display_name == "printer_y_offset":
                value = 10
            elif display_name == "tracking_id":
                value, display_name = 1, "quantity"
            elif display_name == "font":
                value = 1
            elif display_name == "logo":
                raise AgentRenderError("{{logo}} templates are only supported via the agent renderer")
            if display_name not in variables:
                variables[display_name] = {
                    "value": value if _js_truthy(value) else job.get(display_name, _UNDEFINED),
                    "props": [],
                }
            variables[display_name]["props"].append((variable, operations))

        for key, entry in variables.items():
            for variable_id, operations in entry["props"]:
                if key == "font":
                    if operations is None:
                        raise AgentRenderError("agent failed to generate ZPL: {{font}} without dimensions")
                    evaluated = AgentLabelPort.font_zpl(entry["value"], operations)
                elif key == "quantity":
                    evaluated = AgentLabelPort.do_operations("nv-test-123", operations)
                else:
                    evaluated = AgentLabelPort.do_operations(entry["value"], operations)
                template = _js_replace_first(template, variable_id, _js_str(evaluated))
        return template

    @staticmethod
    def font_zpl(value: Any, dimensions: list[str]) -> str:
        zpl = "^AHN," if value == 1 else "^A@N,"
        zpl += ",".join(dimensions)
        if value == 2:
            zpl += ",E:THAI.FNT^FH"
        elif value == 3:
            zpl += ",E:VIET.FNT^FH"
        return zpl

    @staticmethod
    def do_operations(value: Any, operations: list[str] | None) -> Any:
        for op in operations or []:
            if op == "uppercase":
                if not isinstance(value, str):
                    raise AgentRenderError(f"agent failed to generate ZPL: cannot uppercase {_js_str(value)}")
                value = value.upper()
            if "limit" in op:
                if not isinstance(value, str):
                    raise AgentRenderError(f"agent failed to generate ZPL: cannot limit {_js_str(value)}")
                try:
                    n = max(0, int(float(op.split(":")[1])))
                except (IndexError, ValueError):
                    n = 0
                value = _from_utf16_units(_utf16_units(value)[:n])
            if op == "convert_to_zpl_hex":
                value = AgentLabelPort.to_zpl_hex(value)
        return value

    @staticmethod
    def to_zpl_hex(value: Any) -> str:
        if not isinstance(value, str):  # lodash forEach over a non-string iterates nothing
            return ""
        out = []
        for unit in _utf16_units(value):
            if unit == 0x20:
                out.append(" ")
            elif unit < 0x80:
                out.append(f"_{unit:x}")
            elif unit < 0x800:
                out.append(f"_{0xC0 | (unit >> 6):x}_{0x80 | (unit & 0x3F):x}")
            elif unit < 0xD800 or unit >= 0xE000:
                out.append(f"_{0xE0 | (unit >> 12):x}_{0x80 | ((unit >> 6) & 0x3F):x}_{0x80 | (unit & 0x3F):x}")
            else:  # agent's surrogate branch calls str.charCodexAt (typo) and throws
                raise AgentRenderError("agent failed to generate ZPL: text contains characters outside the BMP")
        return "".join(out)


