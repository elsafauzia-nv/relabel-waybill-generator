"""Managed output: one folder per country + vendor + tracking ID, holding only the waybill.

    output/PH_JNT_<TID>/PH_JNT_<TID>.zpl    final, populated ZPL
    output/PH_JNT_<TID>/PH_JNT_<TID>.png    Labelary render of that ZPL
    output/PH_JNT_<TID>/PH_JNT_<TID>.pdf

A run writes into ``output/.staging/`` and only moves its files into the folder once the waybill is complete
and verified, so a failed run never touches an existing waybill.

* same tracking ID scanned again: the folder is kept, its files are deleted and replaced by the new ones;
* tracking ID replaced by a new one: the old folder is renamed to the new name and refilled, so no file or
  folder name of the old tracking ID remains.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prompts import RunInputs
from ws_capture import job_tracking_ids

STAGING_DIR = ".staging"
WAYBILL_PARTS = {"zpl": ".zpl", "png": ".png", "pdf": ".pdf"}  # kind -> suffix
PREVIEW_PARTS = {"preview_zpl": ".PREVIEW-NOT-PRINTED.zpl", "preview_png": ".PREVIEW-NOT-PRINTED.png",
                 "preview_pdf": ".PREVIEW-NOT-PRINTED.pdf"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe(part: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in part)


def record_name(country: str, vendor: str, tracking_id: str) -> str:
    return f"{country}_{vendor}_{safe(tracking_id)}"


@dataclass
class Staged:
    name: str
    dir: Path
    files: dict[str, Path] = field(default_factory=dict)

    def path(self, kind: str) -> Path:
        suffix = WAYBILL_PARTS.get(kind) or PREVIEW_PARTS[kind]
        return self.dir / f"{self.name}{suffix}"

    def write(self, kind: str, data: bytes) -> Path:
        path = self.path(kind)
        path.write_bytes(data)
        self.files[kind] = path
        return path


@dataclass
class CommitResult:
    folder: Path
    saved: dict[str, Path]
    removed: list[Path]  # files deleted (previous waybill in the folder, stray copies)
    renamed_from: Path | None  # old tracking ID's folder, when it was replaced


class OutputStore:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def folder(self, country: str, vendor: str, tracking_id: str) -> Path:
        return self.out_dir / record_name(country, vendor, tracking_id)

    def stray_files(self, country: str, vendor: str, tracking_id: str) -> list[Path]:
        """Files of this tracking ID lying directly in output/ (earlier versions of this script)."""
        tid = re.escape(safe(tracking_id))
        pattern = re.compile(rf"^(?:{re.escape(country)}_{re.escape(vendor)}_)?{tid}(?:_\d{{8}}-\d{{6}})?\..+$", re.I)
        return sorted(p for p in self.out_dir.iterdir() if p.is_file() and pattern.match(p.name))

    def existing(self, inputs: RunInputs) -> dict[str, Any]:
        """What already exists for this country/vendor/tracking ID."""
        folder = self.folder(inputs.country, inputs.vendor, inputs.tracking_id)
        files = sorted(p for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []
        return {
            "folder": folder if folder.is_dir() else None,
            "files": files,
            "stray": self.stray_files(inputs.country, inputs.vendor, inputs.tracking_id),
        }

    def is_taken(self, country: str, vendor: str, tracking_id: str) -> bool:
        found = self.existing(RunInputs(country, vendor, tracking_id))
        return bool(found["folder"] or found["stray"])

    # ----------------------------------------------------------------- writing

    def stage(self, inputs: RunInputs) -> Staged:
        name = record_name(inputs.country, inputs.vendor, inputs.tracking_id)
        directory = self.out_dir / STAGING_DIR / f"{name}-{os.getpid()}"
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        return Staged(name=name, dir=directory)

    def discard(self, staged: Staged) -> None:
        shutil.rmtree(staged.dir, ignore_errors=True)
        try:
            (self.out_dir / STAGING_DIR).rmdir()  # only when empty
        except OSError:
            pass

    def commit(self, staged: Staged, inputs: RunInputs, replaces: str | None) -> CommitResult:
        """Put the staged waybill into its folder, emptying the folder first (the folder itself is kept)."""
        target = self.folder(inputs.country, inputs.vendor, inputs.tracking_id)
        renamed_from = None
        removed: list[Path] = []
        if replaces:
            old = self.folder(inputs.country, inputs.vendor, replaces)
            if old.is_dir() and not target.exists():
                old.rename(target)  # keep the folder, give it the new tracking ID's name
                renamed_from = old
            removed += self.stray_files(inputs.country, inputs.vendor, replaces)
        target.mkdir(parents=True, exist_ok=True)
        removed += [p for p in target.iterdir() if p.is_file()]
        removed += self.stray_files(inputs.country, inputs.vendor, inputs.tracking_id)
        for path in removed:
            path.unlink(missing_ok=True)

        saved = {}
        for kind, path in staged.files.items():
            destination = target / path.name
            os.replace(path, destination)
            saved[kind] = destination
        self.discard(staged)
        return CommitResult(folder=target, saved=saved, removed=removed, renamed_from=renamed_from)

    def save_preview(self, staged: Staged, inputs: RunInputs) -> dict[str, Path]:
        """--blank-missing-fields: put the marked preview files in the folder, next to (never over) a waybill."""
        target = self.folder(inputs.country, inputs.vendor, inputs.tracking_id)
        target.mkdir(parents=True, exist_ok=True)
        for old in target.glob("*.PREVIEW-NOT-PRINTED.*"):
            old.unlink()
        saved = {}
        for kind, path in staged.files.items():
            if kind in PREVIEW_PARTS:
                destination = target / path.name
                os.replace(path, destination)
                saved[kind] = destination
        self.discard(staged)
        return saved


# ----------------------------------------------------------------------------- verification


def verify_staged(staged: Staged, inputs: RunInputs, job: dict[str, Any], zpl_text: str,
                  replaces: str | None) -> tuple[dict[str, Any], list[str]]:
    """Checks on the new waybill before it is committed. Returns (checks, problems)."""
    nv, ext = job_tracking_ids(job)
    template_name = str(job.get("template") or "")
    zpl_bytes = staged.files["zpl"].read_bytes()
    checks: dict[str, Any] = {
        "job_tracking_id_matches_scan": inputs.tracking_id.upper() in {t.upper() for t in (nv, ext) if t},
        "job_country_matches": str(job.get("system_id") or "").upper() == inputs.country,
        "template_matches_vendor": inputs.vendor.lower() in template_name.lower(),
        "file_names_use_tracking_id": all(safe(inputs.tracking_id) in p.name for p in staged.files.values()),
        "tracking_id_printed_on_label": [t for t in (ext, nv) if t and t in zpl_text],
        "zpl_file_matches_populated_text": zpl_bytes in (zpl_text.encode("utf-8"), zpl_text.encode("latin-1", "replace")),
    }
    messages = {
        "job_tracking_id_matches_scan": f"the job's tracking IDs (nv={nv}, ext={ext}) are not {inputs.tracking_id}",
        "job_country_matches": f"the job's system_id is {job.get('system_id')!r}, not {inputs.country}",
        "template_matches_vendor": f"the job's template {template_name!r} is not a {inputs.vendor} template",
        "file_names_use_tracking_id": "a file name does not carry the tracking ID",
        "tracking_id_printed_on_label": "the label does not contain the parcel's tracking ID",
        "zpl_file_matches_populated_text": "the saved .zpl differs from the populated ZPL",
    }
    problems = [msg for key, msg in messages.items() if not checks[key]]
    if replaces:
        in_zpl = replaces in zpl_text and replaces not in str(job.values())
        in_names = any(safe(replaces) in p.name for p in staged.files.values())
        checks["replaced_tracking_id_absent"] = not (in_zpl or in_names)
        if in_zpl:
            problems.append(f"the replaced tracking ID {replaces} appears in the new ZPL")
        if in_names:
            problems.append(f"the replaced tracking ID {replaces} appears in a new file name")
    return checks, problems


def verify_committed(store: OutputStore, inputs: RunInputs, result: CommitResult, replaces: str | None) -> list[str]:
    """After commit: the folder holds exactly the new waybill and nothing of a replaced tracking ID is left."""
    problems = [f"missing after save: {p.name}" for p in result.saved.values() if not p.exists()]
    extra = [p.name for p in result.folder.iterdir() if p not in result.saved.values()]
    if extra:
        problems.append(f"unexpected files left in {result.folder.name}: {', '.join(extra)}")
    if replaces:
        if store.folder(inputs.country, inputs.vendor, replaces).exists():
            problems.append(f"the folder of the replaced tracking ID {replaces} still exists")
        stray = store.stray_files(inputs.country, inputs.vendor, replaces)
        problems += [f"file of the replaced tracking ID still present: {p.name}" for p in stray]
        problems += [f"{p.name} still carries {replaces}" for p in result.folder.iterdir() if safe(replaces) in p.name]
    return problems
