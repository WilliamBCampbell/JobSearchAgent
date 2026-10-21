#!/usr/bin/env python3
"""Exercise the configured inference routes with synthetic evidence, without submissions."""

import json
import time
import httpx
from jobsearch.config import Settings, atomic_write
from jobsearch.db import DB
from jobsearch.corpus import Corpus


def main():
    settings = Settings()
    db = DB(settings)
    with httpx.Client(
        base_url=settings.origin, headers={"X-JSA-Token": settings.token()}, timeout=30
    ) as client:

        def call(method, route, data=None):
            response = client.request(method, "/api" + route, json=data)
            response.raise_for_status()
            return response.json()

        app = next(
            (
                a
                for a in call("GET", "/overview")["applications"]
                if a["requisition"] == "CODEX-EVAL-001"
            ),
            None,
        )
        if not app:
            app = call(
                "POST",
                "/applications",
                {
                    "employer": "Inference Demo",
                    "role": "Software Engineer APIs",
                    "requisition": "CODEX-EVAL-001",
                    "track": "software",
                },
            )
        call("POST", "/applications/" + app["id"] + "/verify", {})
        path = settings.fixture_workspace / "_Research" / "Evaluation-Conflict.md"
        atomic_write(
            path,
            "# Evidence needing review\n\nAn old generated resume calls the synthetic candidate the primary framework developer. A later note explicitly corrects that description to a contributor to the framework. Neither document establishes a skill duration. Ask for confirmation if needed.\n",
        )
        version = Corpus(db).ingest(
            str(path.relative_to(settings.fixture_workspace)), "fixture"
        )["version"]
        requests = [
            {
                "task": "query_expansion",
                "question": "Customer onboarding and API integrations",
            },
            {"task": "requirement_extraction", "application_id": app["id"]},
            {"task": "evidence_selection", "application_id": app["id"]},
            {"task": "fit_assessment", "application_id": app["id"]},
            {
                "task": "draft",
                "application_id": app["id"],
                "question": "Draft one short application paragraph using confirmed API delivery evidence. Omit contact information.",
            },
            {
                "task": "conflict_review",
                "source_versions": [version],
                "question": "How should the conflicting framework ownership descriptions be handled?",
            },
        ]
        report = []
        for request in requests:
            queued = call("POST", "/llm/jobs", request | {"candidate": "fixture"})
            print(request["task"] + ": queued", flush=True)
            deadline = time.monotonic() + 1000
            last = None
            while time.monotonic() < deadline:
                job = call("GET", "/jobs/" + queued["job_id"])
                checkpoint = job["checkpoint"]
                marker = (job["status"], checkpoint.get("stage"))
                if marker != last:
                    print(
                        request["task"]
                        + ": "
                        + job["status"]
                        + " / "
                        + str(checkpoint.get("model", ""))
                        + " / "
                        + str(checkpoint.get("stage", "")),
                        flush=True,
                    )
                    last = marker
                if job["status"] not in ("queued", "running"):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Demonstration timed out")
            entry = {
                "task": request["task"],
                "job_id": job["id"],
                "status": job["status"],
                "error": job.get("error"),
            }
            if job["status"] == "complete":
                run = call("GET", "/llm/runs/" + job["result"]["run_id"])
                entry.update(
                    {
                        "run_id": run["id"],
                        "model": run["model"],
                        "effort": run["effort"],
                        "usage": run["usage"],
                        "output": run["output"],
                    }
                )
            report.append(entry)
            atomic_write(
                settings.state / "codex-demonstration.json",
                json.dumps(report, indent=2, default=str),
            )
        print(
            json.dumps(
                {
                    "completed": sum(r["status"] == "complete" for r in report),
                    "tasks": len(report),
                    "application_id": app["id"],
                    "external_submissions": 0,
                }
            )
        )
        if any(r["status"] != "complete" for r in report):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
