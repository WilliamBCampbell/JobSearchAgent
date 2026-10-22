from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field
from .config import atomic_write, REPO
from .db import js, uid
from .scoring import duplicate_groups


class SearchRequest(BaseModel):
    request_key: str | None = Field(default=None, max_length=120)
    profile: str = "software_dev"
    track: Literal["software", "applied_ai", "solutions"] = "software"
    mode: Literal["fixture", "live"] = "fixture"
    terms: list[str] | None = Field(default=None, max_length=6)
    sites: list[
        Literal[
            "indeed",
            "linkedin",
            "glassdoor",
            "zip_recruiter",
            "bayt",
            "naukri",
            "bdjobs",
            "hellowork",
        ]
    ] = Field(default_factory=lambda: ["indeed", "linkedin"])
    results: int = Field(default=15, ge=1, le=100)
    hours: int = Field(default=168, ge=1, le=8760)
    remote: bool = True
    timeout: int = Field(default=60, ge=1, le=180)


class SearchService:
    def __init__(self, db):
        self.db = db

    def adapter(self):
        from . import search_planning

        return search_planning

    def profiles(self):
        path = self.db.settings.jobspy / "search_profiles.json"
        if path.is_file():
            return json.loads(path.read_text())
        from copy import deepcopy
        from .search_planning import DEFAULT_PROFILES

        return deepcopy(DEFAULT_PROFILES)

    def health(self):
        official = self.db.settings.jobspy_backend == "official_mcp"
        python = (
            self.db.settings.jobspy_mcp_python
            if official
            else self.db.settings.jobspy / ".venv/bin/python"
        )
        result = (
            subprocess.run(
                [str(python), "-m", "pip", "check"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if python.exists()
            else None
        )
        commit = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.db.settings.jobspy,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if not official and self.db.settings.jobspy.is_dir()
            else None
        )
        versions = (
            subprocess.run(
                [
                    str(python),
                    "-c",
                    "import json; from importlib.metadata import version; print(json.dumps({k:version(k) for k in ['python-jobspy','mcp-jobspy','mcp']}))",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if official and python.exists()
            else None
        )
        from .official_jobspy import VERSION, SDK_VERSION

        installed = (
            json.loads(versions.stdout) if versions and versions.returncode == 0 else {}
        )
        pinned = not official or installed == {
            "python-jobspy": VERSION,
            "mcp-jobspy": VERSION,
            "mcp": SDK_VERSION,
        }
        return {
            "available": bool(result and result.returncode == 0 and pinned),
            "backend": self.db.settings.jobspy_backend,
            "versions": installed,
            "version_check": "ok"
            if pinned
            else "Install the pinned official MCP environment",
            "dependency_check": result.stdout.strip()
            if result
            else "JobSpy environment missing",
            "source_commit": commit.stdout.strip() if commit and not official else None,
            "live_enabled": self.db.settings.live_search
            or self.db.setting("live_search", False),
            "capabilities": {
                "indeed": {"remote_filter": "requested; verify official policy"},
                "linkedin": {
                    "remote_filter": "ignored",
                    "city_label": "discovery only",
                },
                "glassdoor": {"availability": "unverified; previous timeout"},
                "zip_recruiter": {"availability": "unverified; previous HTTP 403"},
            },
            "concurrency": 1,
            "timeout_max_seconds": 180,
            "max_query_attempts": 2,
        }

    def start(self, request: SearchRequest):
        if request.profile not in self.profiles()["profiles"]:
            raise ValueError("Unknown search profile")
        if request.mode == "live" and not (
            self.db.settings.live_search or self.db.setting("live_search", False)
        ):
            raise ValueError(
                "Live search is disabled. Enable it in local configuration first."
            )
        from .queue import enqueue

        if request.request_key:
            with self.db.tx() as c:
                c.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))", (request.request_key,)
                )
                old = c.execute(
                    "SELECT id,status,input FROM jobs WHERE kind='search' AND input->>'request_key'=%s",
                    (request.request_key,),
                ).fetchone()
                if old:
                    if old["input"] != request.model_dump():
                        raise ValueError(
                            "Search request key already belongs to different parameters"
                        )
                    return {"job_id": old["id"], "status": old["status"]}
                from .db import uid

                jid = uid()
                c.execute(
                    "INSERT INTO jobs(id,kind,input) VALUES(%s,'search',%s)",
                    (jid, js(request.model_dump())),
                )
                return {"job_id": jid, "status": "queued"}
        return enqueue(self.db, "search", request.model_dump())

    def progress(self, job_id):
        job = self.db.one("SELECT * FROM jobs WHERE id=%s AND kind='search'", (job_id,))
        if not job:
            raise ValueError("Search not found")
        return job

    def results(self, job_id, offset=0, limit=20):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid page")
        job = self.progress(job_id)
        folder = Path(
            job["checkpoint"].get(
                "folder", job["result"].get("folder", self.db.settings.state / "absent")
            )
        )
        path = folder / "jobs.json"
        manifest = folder / "manifest.json"
        rows = json.loads(path.read_text()) if path.is_file() else []
        return {
            "job_id": job_id,
            "status": job["status"],
            "offset": offset,
            "total": len(rows),
            "results": rows[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "manifest": json.loads(manifest.read_text()) if manifest.exists() else {},
        }

    def cancel(self, job_id):
        self.progress(job_id)
        self.db.execute(
            "UPDATE jobs SET cancel_requested=true,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=%s",
            (job_id,),
        )
        return {"job_id": job_id, "cancel_requested": True}

    def run(self, job, checkpoint, cancelled):
        req = SearchRequest(**job["input"])
        adapter = self.adapter()
        args = argparse.Namespace(
            profile=req.profile,
            term=req.terms,
            sites=req.sites,
            location=None,
            remote=req.remote,
            results=req.results,
            hours=req.hours,
            descriptions=True,
        )
        plan = adapter.build_plan(args, self.profiles())
        # Fixtures never write into the real research directory.
        root = (
            self.db.settings.fixture_workspace
            if req.mode == "fixture"
            else self.db.settings.workspace
        )
        folder = root / "_Research" / ("Agent-" + job["id"])
        folder.mkdir(parents=True, exist_ok=True)
        manifest_path = folder / "manifest.json"
        manifest = (
            json.loads(manifest_path.read_text())
            if manifest_path.exists()
            else {
                "job_id": job["id"],
                "mode": req.mode,
                "queries": [],
                "started_at": datetime.now(timezone.utc).isoformat(),
                "status": "running",
                "track": req.track,
                "backend": "fixture"
                if req.mode == "fixture"
                else self.db.settings.jobspy_backend,
                "upstream_version": "1.4.0"
                if req.mode == "live"
                and self.db.settings.jobspy_backend == "official_mcp"
                else None,
            }
        )
        all_rows = []
        for entry in manifest["queries"]:
            all_rows.extend(
                json.loads((folder / entry["raw"]).read_text()).get("jobs", [])
            )
        fixture = (
            json.loads((REPO / "fixtures/search.json").read_text())
            if req.mode == "fixture"
            else None
        )
        for index, params in enumerate(plan):
            if index < len(manifest["queries"]):
                continue
            if cancelled():
                manifest["status"] = "cancelled"
                break
            site = params["site_name"][0]
            effective = params.copy()
            warnings = []
            backend = manifest.get("backend", "legacy")
            if backend not in ("fixture", "legacy", "official_mcp"):
                raise ValueError("Unknown saved search backend")
            if site == "linkedin" and backend != "official_mcp":
                effective.pop("is_remote", None)
                warnings.append(
                    "LinkedIn remote filter is ignored by this checkout; verify employer policy"
                )
            entry = {
                "source": site,
                "query": params["search_term"],
                "requested_filters": params,
                "effective_filters": effective,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "raw": f"{index:03d}-{site}.json",
                "warnings": warnings,
                "errors": [],
                "attempts": [],
                "backend": backend,
            }
            payload = {"jobs": [], "messages": []}
            for attempt in range(2):
                if cancelled():
                    break
                if fixture is not None:
                    payload = fixture.get(site, {"jobs": [], "messages": []})
                    code = 0
                else:
                    if backend == "official_mcp":
                        if not self.db.settings.jobspy_mcp_python.is_file():
                            raise ValueError(
                                "Install the official JobSpy MCP before searching"
                            )
                        command = [sys.executable, "-m", "jobsearch.official_jobspy"]
                        request = {
                            "python": str(self.db.settings.jobspy_mcp_python),
                            "parameters": params,
                            "output": str(
                                folder / f"{index:03d}-{site}-official-{uid()}.json"
                            ),
                        }
                    else:
                        command = [
                            str(self.db.settings.jobspy / ".venv/bin/python"),
                            str(self.db.settings.jobspy / "search_jobs.py"),
                            "--worker",
                        ]
                        request = params
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=REPO
                        if backend == "official_mcp"
                        else self.db.settings.jobspy,
                        start_new_session=True,
                    )
                    process.stdin.write(json.dumps(request).encode())
                    process.stdin.close()
                    process.stdin = None
                    deadline = time.monotonic() + req.timeout
                    while True:
                        try:
                            stdout, stderr = process.communicate(timeout=0.3)
                            code = process.returncode
                            break
                        except subprocess.TimeoutExpired:
                            if cancelled() or time.monotonic() > deadline:
                                os.killpg(process.pid, signal.SIGTERM)
                                try:
                                    stdout, stderr = process.communicate(timeout=2)
                                except subprocess.TimeoutExpired:
                                    os.killpg(process.pid, signal.SIGKILL)
                                    stdout, stderr = process.communicate()
                                code = -1
                                entry["errors"].append(
                                    "cancelled" if cancelled() else "timeout"
                                )
                                break
                    atomic_write(
                        folder / f"{index:03d}-{site}-attempt-{attempt}.log", stderr
                    )
                    try:
                        payload = (
                            json.loads(stdout)
                            if code == 0
                            else {"jobs": [], "messages": []}
                        )
                    except ValueError:
                        entry["errors"].append("parser_failure")
                        payload = {"jobs": [], "messages": []}
                messages = payload.get("messages", [])
                entry["upstream_errors"] = payload.get("upstream_errors", {})
                entry["upstream_warnings"] = payload.get("upstream_warnings", {})
                source_warnings = entry["upstream_warnings"].get(site, [])
                entry["warnings"].extend(source_warnings)
                if site == "linkedin" and any(
                    "is_remote" in warning and "ignor" in warning
                    for warning in source_warnings
                ):
                    effective.pop("is_remote", None)
                text = " ".join(m["message"] for m in messages)
                if "429" in text:
                    entry["errors"].append("rate_limited")
                if (
                    "403" in text
                    or "CAPTCHA" in text.upper()
                    or "blocked" in text.lower()
                ):
                    entry["errors"].append("source_unavailable")
                errors = any(m.get("level") in ("ERROR", "CRITICAL") for m in messages)
                if code and not entry["errors"]:
                    entry["errors"].append("source_unavailable")
                entry["attempts"].append(
                    {
                        "attempt": attempt + 1,
                        "exit_code": code,
                        "messages": messages,
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                # Backoff for transient empty failures only; do not repeatedly hit blocked/rate-limited sources.
                if (
                    fixture is not None
                    or payload["jobs"]
                    or not errors
                    or any(
                        e in entry["errors"]
                        for e in ["rate_limited", "source_unavailable", "cancelled"]
                    )
                ):
                    break
                for _ in range(20):
                    if cancelled():
                        break
                    time.sleep(0.1)
            rows = [
                r
                | {
                    "search_term": params["search_term"],
                    "scraped_at": entry["started_at"],
                }
                for r in payload["jobs"]
            ]
            entry["missing_descriptions"] = sum(not r.get("description") for r in rows)
            if entry["missing_descriptions"]:
                entry["warnings"].append(
                    f"{entry['missing_descriptions']} results lack descriptions"
                )
            failed = bool(entry["errors"]) or any(
                m.get("level") in ("ERROR", "CRITICAL")
                for m in payload.get("messages", [])
            )
            entry["status"] = (
                "partial"
                if failed and rows
                else "failed"
                if failed
                else "ok"
                if rows
                else "empty"
            )
            entry["finished_at"] = datetime.now(timezone.utc).isoformat()
            entry["count"] = len(rows)
            atomic_write(
                folder / entry["raw"], json.dumps(payload | {"jobs": rows}, indent=2)
            )
            all_rows.extend(rows)
            manifest["queries"].append(entry)
            atomic_write(manifest_path, json.dumps(manifest, indent=2))
            grouped = duplicate_groups(
                adapter.deduplicate(all_rows),
                self.db.all(
                    "SELECT * FROM applications WHERE candidate=%s",
                    ("fixture" if req.mode == "fixture" else "real",),
                ),
            )
            atomic_write(folder / "jobs.json", json.dumps(grouped, indent=2))
            checkpoint(
                {
                    "folder": str(folder),
                    "queries_completed": index + 1,
                    "queries_total": len(plan),
                    "results": len(grouped),
                }
            )
        failed = any(q["status"] in ("partial", "failed") for q in manifest["queries"])
        manifest.update(
            status="cancelled"
            if cancelled()
            else "partial"
            if failed and all_rows
            else "failed"
            if failed
            else "complete",
            finished_at=datetime.now(timezone.utc).isoformat(),
            raw_count=len(all_rows),
        )
        atomic_write(manifest_path, json.dumps(manifest, indent=2))
        return {"folder": str(folder), "manifest": manifest}
