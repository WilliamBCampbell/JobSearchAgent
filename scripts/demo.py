#!/usr/bin/env python3
"""Exercise the authenticated local workflow. Approvals apply only to synthetic fixtures."""

import json
import time
import httpx
from jobsearch.config import Settings, atomic_write

s = Settings()
client = httpx.Client(base_url=s.origin, headers={"X-JSA-Token": s.token()}, timeout=40)


def call(path, data=None, method=None):
    r = client.request(
        method or ("GET" if data is None else "POST"), "/api" + path, json=data
    )
    r.raise_for_status()
    return r.json()


def wait(job, limit=600):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        j = call("/jobs/" + job["job_id"])
        if j["status"] in ("complete", "failed", "unresolved", "cancelled"):
            if j["status"] != "complete":
                raise RuntimeError("Job stopped: " + json.dumps(j, default=str))
            return j
        time.sleep(0.5)
    raise TimeoutError("Local worker did not finish the fixture job")


call("/fixtures/seed", {})
search = wait(
    call(
        "/search",
        {
            "profile": "software_dev",
            "track": "software",
            "terms": ["API engineer"],
            "mode": "fixture",
        },
    )
)
results = call("/search/" + search["id"] + "/results")
print(
    "Search:",
    len(results["results"]),
    "groups;",
    results["manifest"]["status"],
    flush=True,
)
apps = call("/overview")["applications"]
existing = next(
    (a for a in apps if a["requisition"] == "NS-101" and not a["imported"]), None
)
if existing:
    a = existing
else:
    a = call(
        "/applications",
        {
            "employer": "Northstar Demo",
            "role": "Software Engineer, APIs",
            "requisition": "NS-101",
            "track": "software",
            "source": results["results"][0]["sources"],
        },
    )
aid = a["id"]
if a["state"] == "discovered":
    call("/applications/" + aid + "/verify", {})
    a = call("/applications/" + aid)
if a["state"] == "verified":
    call("/applications/" + aid + "/fixture-score", {})
    a = call("/applications/" + aid)
if a["state"] == "scored":
    call("/applications/" + aid + "/select", {})
    a = call("/applications/" + aid)
if a["state"] == "selected":
    wait(call("/applications/" + aid + "/prepare", {}))
    a = call("/applications/" + aid)
if a["state"] in ("prepared", "missing_answers"):
    assert a["candidate"] == "fixture"
    if (
        a.get("package_review", {}).get("version")
        and not a["package_review"]["reviewed"]
    ):
        call(
            "/applications/" + aid + "/package-review",
            {"version": a["package_review"]["version"]},
        )
    wait(call("/applications/" + aid + "/continue", {}))
    a = call("/applications/" + aid)
if a["state"] == "missing_answers":
    queued = None
    for q in a["questions"]:
        if q["answer_fact"]:
            continue
        value = {"salary": "120000", "travel": "Up to 10%"}.get(q["key"])
        if value is None:
            raise RuntimeError("Unexpected fixture question: " + q["key"])
        response = call(
            "/questions/" + q["id"] + "/answer",
            {"value": value, "scope": "application"},
        )
        if "job_id" in response:
            queued = response
    if queued:
        wait(queued)
    a = call("/applications/" + aid)
if a["state"] == "review":
    review = call("/applications/" + aid + "/review")
    assert (
        review["payload"]["employer"] == "Northstar Demo"
        and review["payload"]["requisition"] == "NS-101"
    )
    print(
        "Fixture review:",
        len(review["payload"]["answers"]),
        "answers;",
        len(review["payload"]["attachments"]),
        "actual attachment",
        flush=True,
    )
    wait(
        call(
            "/applications/" + aid + "/approve",
            {"approve": True, "reviewed_hash": review["hash"]},
        )
    )
a = call("/applications/" + aid)
assert a["state"] == "submitted" and a["receipts"], a["state"]
tracker = call("/tracker?candidate=fixture")
preview = call("/tracker/preview/" + aid)
synced = call(
    "/tracker/update/" + aid,
    {"expected_hash": tracker["hash"], "reviewed_hash": preview["review_hash"]},
)
report = {
    "application_id": aid,
    "state": a["state"],
    "search_job": search["id"],
    "manifest_status": results["manifest"]["status"],
    "receipt_count": len(a["receipts"]),
    "submitted_attachments": [x["kind"] for x in a["artifacts"] if x["attached"]],
    "tracker_totals": synced["totals"],
    "api_origin": s.origin,
    "external_submissions": 0,
}
atomic_write(s.state / "demonstration.json", json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
