"""Render ZPL through the Labelary API (the service behind https://labelary.com/viewer.html)."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from errors import Failure, RunError

API_URL = "https://api.labelary.com/v1/printers/{dpmm}dpmm/labels/{w}x{h}/{index}"
_DIM = re.compile(rb"\^(PW|LL)\s*(\d+)")
_DPI = {6: 152, 8: 203, 12: 300, 24: 600}  # what Labelary uses per dpmm
_RETRY_STATUS = {429, 500, 502, 503, 504}


_TEMPLATE_MM = re.compile(r"(\d{2,3})X(\d{2,3})", re.I)  # e.g. hub_shipping_label_100X150_cross_border_jnt


@dataclass
class Rendered:
    path: Path
    url: str
    lint: list[dict]


@dataclass(frozen=True)
class Geometry:
    width_in: float
    height_in: float
    dpmm: int
    source: str


def _inches(value: float) -> float:
    return min(math.ceil(value * 100) / 100, 15.0)  # rounded up so no dots are clipped; Labelary max 15 in


def label_geometry(zpl: bytes, template_name: str | None, dpmm_override: int | None = None) -> Geometry:
    """Label size and resolution from the template's own configuration.

    The printer template name carries the label size in mm (``..._100X150_...``) and the ZPL its width in
    dots (``^PW800``): 800 dots / 100 mm = 8 dpmm (203 dpi). Falls back to ^PW/^LL at 8 dpmm, then 4x6 in.
    """
    dims = {k.decode(): int(v) for k, v in _DIM.findall(zpl)}
    mm = _TEMPLATE_MM.search(template_name or "")
    if mm:
        w_mm, h_mm = int(mm.group(1)), int(mm.group(2))
        dpmm = dpmm_override or (min(_DPI, key=lambda d: abs(d - dims["PW"] / w_mm)) if "PW" in dims else 8)
        source = f"template {template_name}: {w_mm}x{h_mm} mm" + (f", ^PW{dims['PW']}" if "PW" in dims else "")
        # Cover the template's dots too: Labelary uses 203 dpi for 8 dpmm, so 100 mm alone is 799 dots < ^PW800.
        w = max(w_mm / 25.4, dims.get("PW", 0) / _DPI[dpmm])
        h = max(h_mm / 25.4, dims.get("LL", 0) / _DPI[dpmm])
        return Geometry(_inches(w), _inches(h), dpmm, f"{source} -> {dpmm} dpmm")
    dpmm = dpmm_override or 8
    if "PW" in dims or "LL" in dims:
        w = _inches(dims["PW"] / _DPI[dpmm]) if "PW" in dims else 4.0
        h = _inches(dims["LL"] / _DPI[dpmm]) if "LL" in dims else 6.0
        return Geometry(w, h, dpmm, f"^PW/^LL at {dpmm} dpmm")
    return Geometry(4.0, 6.0, dpmm, "default 4x6 in")


def parse_lint(header: str | None) -> list[dict]:
    """X-Warnings is pipe-separated groups of: byte index | length | command | parameter | message."""
    if not header:
        return []
    parts = header.split("|")
    lint = []
    for i in range(0, len(parts) - 4, 5):
        try:
            offset = int(parts[i])
        except ValueError:
            continue
        lint.append({"offset": offset, "command": parts[i + 2] or "text", "message": parts[i + 4]})
    return lint


def format_lint(entry: dict) -> str:
    return f"{entry['command']} at byte {entry['offset']}: {entry['message']}"


def render(zpl: bytes, out_path: Path, fmt: str, geometry: Geometry, retries: int = 3) -> Rendered:
    """fmt "png" renders the first label; "pdf" renders every label in the ZPL."""
    g = geometry
    url = API_URL.format(dpmm=g.dpmm, w=g.width_in, h=g.height_in, index="0/" if fmt == "png" else "")
    accept = "image/png" if fmt == "png" else "application/pdf"
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                url,
                data=zpl,
                headers={"Accept": accept, "Content-Type": "application/x-www-form-urlencoded", "X-Linter": "On"},
                timeout=60,
            )
        except requests.RequestException as e:
            last_error = f"network error calling Labelary: {e}"
        else:
            if resp.status_code == 200 and resp.content:
                out_path.write_bytes(resp.content)
                return Rendered(out_path, url, parse_lint(resp.headers.get("X-Warnings")))
            last_error = f"Labelary returned HTTP {resp.status_code}: {resp.text[:300]}"
            if resp.status_code not in _RETRY_STATUS:
                break
        time.sleep(1.5 * attempt)
    raise RunError(Failure.LABELARY, last_error, "the .zpl file is saved; open it in https://labelary.com/viewer.html")
