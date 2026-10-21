#!/usr/bin/env python3
"""Local agent for the waybill web UI (GitHub Pages): runs the existing capture workflow on this Mac.

    ./agent.py                      # http://127.0.0.1:8765, accepts the GitHub Pages UI origin
    ./agent.py --allow-origin http://127.0.0.1:8766    # also accept a locally served copy of the UI

Restricted local API (bound to 127.0.0.1 only):
    GET  /api/health                     agent status, supported countries/vendors
    POST /api/jobs                       {country, vendor, tracking_id, no_print, duplicate?, replace_with?}
    GET  /api/jobs/<id>                  stages, log, result (ZPL text, file links, checks)
    GET  /api/jobs/<id>/files/<kind>     kind = zpl | png | pdf of that job's waybill only

Requests must come from an allowed Origin and carry a 127.0.0.1/localhost Host header. No file paths,
shell commands, browser control, tokens or captured payloads are exposed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_VENV_PYTHON = Path(__file__).resolve().parent / ".venv" / "bin" / "python"
if _VENV_PYTHON.exists() and Path(sys.prefix).resolve() != _VENV_PYTHON.parent.parent.resolve():
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from typing import Any  # noqa: E402

from aiohttp import web  # noqa: E402

import artifacts  # noqa: E402
import capture_waybill  # noqa: E402
import prompts  # noqa: E402
import report  # noqa: E402
from errors import RunError  # noqa: E402

VERSION = "1.0"
DEFAULT_PORT = 8765
DEFAULT_ORIGINS = ["https://elsafauzia-nv.github.io"]
FILE_KINDS = {"zpl": "text/plain; charset=utf-8", "png": "image/png", "pdf": "application/pdf"}
MAX_BODY = 2048
JOB_FIELDS = {"country", "vendor", "tracking_id", "no_print", "duplicate", "replace_with"}
KEEP_JOBS = 20


class Job(report.Reporter):
    """One workflow run; records what the web UI shows (it is also the run's reporter)."""

    def __init__(self, inputs: prompts.RunInputs, no_print: bool, replaces: str | None) -> None:
        self.id = uuid.uuid4().hex
        self.inputs = inputs
        self.no_print = no_print
        self.replaces = replaces
        self.state = "running"
        self.started = time.time()
        self.stages: dict[str, dict[str, Any]] = {name: {"state": "pending", "detail": ""} for name in report.STAGES}
        self.log: list[dict[str, str]] = []
        self.data: dict[str, Any] = {}  # what report.result() delivered: ZPL text, files, checks, error
        self._current = "agent"

    # report.Reporter
    def line(self, kind: str, text: str) -> None:
        if text.strip():
            self.log.append({"kind": kind, "text": text.strip()})

    def stage(self, name: str, state: str, detail: str = "") -> None:
        name = self._current if name == "*" else name
        if name not in self.stages:
            return
        entry = self.stages[name]
        entry["state"] = state
        if detail:
            entry["detail"] = detail
        entry["at"] = time.time()
        if state == "running":
            self._current = name

    def result(self, **data: Any) -> None:
        self.data.update(data)

    def public(self) -> dict[str, Any]:
        """What the web UI may see: no local paths, payloads or tokens."""
        result = {k: v for k, v in self.data.items() if k not in ("files", "folder")}
        files = self.data.get("files") or {}
        result["files"] = {kind: f"/api/jobs/{self.id}/files/{kind}" for kind in files if kind in FILE_KINDS}
        if self.data.get("folder"):
            result["folder"] = Path(self.data["folder"]).name
        return {
            "id": self.id,
            "state": self.state,
            "country": self.inputs.country,
            "vendor": self.inputs.vendor,
            "tracking_id": self.inputs.tracking_id,
            "replaces": self.replaces,
            "no_print": self.no_print,
            "stages": [{"name": n, **self.stages[n]} for n in report.STAGES],
            "log": self.log[-200:],
            "result": result,
        }


class Agent:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.origins = set(args.allow_origin)
        self.hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
        self.store = artifacts.OutputStore(capture_waybill.HERE / "output")
        self.jobs: dict[str, Job] = {}
        self.running: Job | None = None

    # ------------------------------------------------------------------ security middleware

    @web.middleware
    async def guard(self, request: web.Request, handler):
        if request.host not in self.hosts:
            return web.json_response({"error": "forbidden host"}, status=403)
        origin = request.headers.get("Origin")
        if origin is not None and origin not in self.origins:
            return web.json_response({"error": "origin not allowed"}, status=403)
        if origin is None and request.path != "/api/health":
            return web.json_response({"error": "requests must come from the waybill web UI"}, status=403)
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        else:
            try:
                response = await handler(request)
            except web.HTTPException as e:
                response = web.json_response({"error": e.reason}, status=e.status)
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Allow-Private-Network"] = "true"  # Chrome Private Network Access
            response.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    # ------------------------------------------------------------------ routes

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "agent": "waybill-zpl-capture",
            "version": VERSION,
            "busy": self.running is not None,
            "countries": {c: list(v) for c, v in prompts.COUNTRY_VENDORS.items()},
        })

    async def create_job(self, request: web.Request) -> web.Response:
        if request.content_length is None or request.content_length > MAX_BODY:
            return web.json_response({"error": "request body missing or too large"}, status=413)
        try:
            body = json.loads(await request.text())
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict) or set(body) - JOB_FIELDS:
            return web.json_response({"error": f"only these fields are accepted: {', '.join(sorted(JOB_FIELDS))}"}, status=400)
        try:
            country = str(body.get("country", "")).upper()
            vendor = str(body.get("vendor", "")).upper()
            prompts.validate_combination(country, vendor)
            inputs = prompts.RunInputs(country, vendor, prompts.validate_tracking_id(str(body.get("tracking_id", ""))))
            no_print = body.get("no_print", False)
            duplicate = body.get("duplicate")
            if not isinstance(no_print, bool) or duplicate not in (None, "regenerate", "replace"):
                raise RunError(prompts.Failure.INPUT, "no_print must be true/false; duplicate must be regenerate or replace")
            replaces = None
            if duplicate == "replace":
                new_tid = prompts.validate_tracking_id(str(body.get("replace_with", "")))
                if new_tid.upper() == inputs.tracking_id.upper():
                    raise RunError(prompts.Failure.INPUT, "the new tracking ID is the same as the old one")
                if self.store.is_taken(country, vendor, new_tid):
                    raise RunError(prompts.Failure.INPUT, f"{new_tid} already has its own waybill; choose another tracking ID")
                replaces, inputs = inputs.tracking_id, prompts.RunInputs(country, vendor, new_tid)
        except RunError as e:
            return web.json_response({"error": e.message}, status=400)

        if self.running is not None:
            return web.json_response({"error": "busy", "job_id": self.running.id}, status=409)
        if duplicate is None:
            found = self.store.existing(inputs)
            if found["folder"] or found["stray"]:
                return web.json_response({
                    "error": "duplicate",
                    "tracking_id": inputs.tracking_id,
                    "folder": artifacts.record_name(country, vendor, inputs.tracking_id),
                    "files": [p.name for p in found["files"] + found["stray"]],
                }, status=409)

        summary = "no duplicate" if duplicate is None else (
            f"replacing {replaces} with {inputs.tracking_id}" if replaces else f"regenerating {inputs.tracking_id}")
        plan = prompts.DuplicatePlan(inputs, replaces, summary)
        job = Job(inputs, no_print, replaces)
        self.jobs[job.id] = job
        for old in list(self.jobs)[:-KEEP_JOBS]:
            self.jobs.pop(old, None)
        self.running = job
        asyncio.get_running_loop().create_task(self._run(job, plan))
        return web.json_response({"job_id": job.id}, status=202)

    async def _run(self, job: Job, plan: prompts.DuplicatePlan) -> None:
        token = report.use(job)
        try:
            job.stage("agent", "ok", "Local agent accepted the job")
            argv = ["--country", job.inputs.country, "--vendor", job.inputs.vendor, "--tid", job.inputs.tracking_id]
            argv += ["--no-print"] if job.no_print else []
            # The printer app's own label.js is used when installed; otherwise the verified Python copy.
            argv += ["--allow-python-renderer"]
            args = capture_waybill.parse_args(argv)
            code = await capture_waybill.execute(self.store, job.inputs, plan, args)
            job.state = "done" if code == 0 else "failed"
            if code != 0 and not any(s["state"] == "failed" for s in job.stages.values()):
                job.stage("*", "failed", job.data.get("error") or f"workflow ended with exit code {code}")
        except Exception as e:  # never leave the UI waiting on a crashed job
            job.state = "failed"
            job.stage("*", "failed", f"internal error: {type(e).__name__}: {e}")
            job.data.setdefault("error", f"internal error: {type(e).__name__}")
        finally:
            report._current.reset(token)
            self.running = None

    async def get_job(self, request: web.Request) -> web.Response:
        job = self.jobs.get(request.match_info["job_id"])
        if not job:
            return web.json_response({"error": "unknown job"}, status=404)
        return web.json_response(job.public())

    async def get_file(self, request: web.Request) -> web.StreamResponse:
        job = self.jobs.get(request.match_info["job_id"])
        kind = request.match_info["kind"]
        path = Path((job.data.get("files") or {}).get(kind, "")) if job and kind in FILE_KINDS else None
        out_dir = self.store.out_dir.resolve()
        if not path or not path.is_file() or out_dir not in path.resolve().parents:
            return web.json_response({"error": "file not available"}, status=404)
        return web.FileResponse(path, headers={
            "Content-Type": FILE_KINDS[kind],
            "Content-Disposition": f'attachment; filename="{path.name}"',
        })

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self.guard], client_max_size=MAX_BODY)
        app.router.add_get("/api/health", self.health)
        app.router.add_post("/api/jobs", self.create_job)
        app.router.add_get("/api/jobs/{job_id:[0-9a-f]{32}}", self.get_job)
        app.router.add_get("/api/jobs/{job_id:[0-9a-f]{32}}/files/{kind}", self.get_file)
        for path in ("/api/health", "/api/jobs", "/api/jobs/{job_id:[0-9a-f]{32}}",
                     "/api/jobs/{job_id:[0-9a-f]{32}}/files/{kind}"):
            app.router.add_route("OPTIONS", path, self.health)  # answered by the middleware
        return app


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--allow-origin", action="append", default=None,
                   help=f"web UI origin allowed to call the agent (default {DEFAULT_ORIGINS[0]}); repeatable")
    args = p.parse_args()
    args.allow_origin = args.allow_origin or DEFAULT_ORIGINS
    agent = Agent(args)
    print(f"Waybill agent {VERSION} on http://127.0.0.1:{args.port}")
    print(f"Accepting the web UI from: {', '.join(sorted(agent.origins))}")
    print(f"Output folder: {agent.store.out_dir}")
    print("Keep this window open while using the web UI. Ctrl+C stops the agent.")
    web.run_app(agent.app(), host="127.0.0.1", port=args.port, print=None, access_log=None)


if __name__ == "__main__":
    main()
