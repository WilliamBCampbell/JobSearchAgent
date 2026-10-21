from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from jobsearch.db import js, uid
from jobsearch.forms import FormSpec, FieldSpec
from jobsearch.greenhouse_transport import GreenhouseTransport
from jobsearch.workflow import ApprovalInput
from .test_workflow_tracker import ready


async def test_native_storage_root_matches_chromium_request(monkeypatch):
    storage = "https://grnhse-prod-jben-us-east-1.s3.amazonaws.com"
    presign = "https://boards.greenhouse.io/uncacheable_attributes/presigned_fields"
    client = AsyncMock()
    client.get.return_value = httpx.Response(
        200, json={"url": storage}, request=httpx.Request("GET", presign)
    )
    client.__aenter__.return_value = client
    monkeypatch.setattr(
        "jobsearch.greenhouse_transport.httpx.AsyncClient", lambda **kw: client
    )
    transport = GreenhouseTransport(
        SimpleNamespace(),
        {"candidate": "real"},
        SimpleNamespace(url="https://job-boards.greenhouse.io/example/jobs/123"),
        {},
        {},
    )
    transport.check = lambda: None
    route = SimpleNamespace(
        request=SimpleNamespace(url=presign, method="GET"), fulfill=AsyncMock()
    )
    assert await transport.handle(route, {"phase": "inspect"})
    # Do not add a slash to native metadata: its fileUrl() appends "/<key>".
    assert route.fulfill.call_args.kwargs["json"]["url"] == storage
    assert transport.upload_url == storage + "/"
    # Chromium supplies "/" even when the original metadata omitted it.
    # Before an attempt, the recognized upload must be explicitly blocked.
    upload = SimpleNamespace(
        request=SimpleNamespace(url=storage + "/", method="POST", headers={}),
        abort=AsyncMock(),
    )
    assert await transport.handle(upload, {"phase": "fill"})
    upload.abort.assert_awaited_once()


def stopped(db):
    flow, aid, path = ready(db)
    app = flow.get(aid)
    spec = FormSpec(
        adapter="greenhouse",
        url="http://fixture.invalid/jobs/123",
        requisition=app["requisition"],
        fields=[
            FieldSpec(key=k, name=k, label=k, type="text")
            for k in app["payload"]["answers"]
        ]
        + [
            FieldSpec(
                key="resume", name="resume", label="Resume", type="file", required=True
            )
        ],
    )
    flow.update_payload(
        aid,
        {
            "form_plan": {"spec": spec.model_dump(), "fingerprint": spec.fingerprint},
            "consents": {"employer_notices": []},
        },
    )
    flow.update_payload(aid, {"browser_hash": flow.review(aid)["hash"]})
    flow.authorize(
        aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
    )
    attempt = flow.begin_attempt(aid, mark_attached=False)
    run = db.one("SELECT result FROM attempts WHERE id=%s", (attempt,))["result"][
        "run_id"
    ]
    flow.unresolved(aid, "Upload stopped before final request")
    db.audit(
        "greenhouse.attempt_finished",
        aid,
        {
            "attempt_id": attempt,
            "run_id": run,
            "post_observed": False,
            "post_status": None,
        },
    )
    db.execute("UPDATE jobs SET status='failed' WHERE kind='submit'")
    return flow, aid, attempt, path


@pytest.mark.parametrize("legacy_record", [False, True])
def test_safe_retry_retains_approval_attempt_and_clears_old_run_proof(
    db, legacy_record
):
    flow, aid, attempt, _ = stopped(db)
    if legacy_record:
        db.execute("UPDATE attempts SET result=result - 'run_id' - 'send_started'")
        db.execute(
            "UPDATE audit SET detail=detail - 'run_id' WHERE event='greenhouse.attempt_finished'"
        )
    assert flow.submission_recovery(aid)["can_retry"]
    flow.retry_submission(aid)
    assert flow.get(aid)["state"] == "authorized"
    assert not db.all(
        "SELECT 1 FROM followups WHERE category='manual_application' AND NOT resolved"
    )
    assert flow.begin_attempt(aid, mark_attached=False) == attempt
    assert db.one("SELECT count(*) n FROM approvals")["n"] == 1
    assert db.one("SELECT count(*) n FROM attempts")["n"] == 1
    # A crash in the new run cannot borrow the old run's completed-stop evidence.
    flow.unresolved(aid, "Worker disappeared")
    db.execute("UPDATE jobs SET status='failed'")
    assert not flow.submission_recovery(aid, validate=True)["can_retry"]


@pytest.mark.parametrize(
    "blocker",
    [
        "post_started",
        "post_observed",
        "wrong_run",
        "no_stop",
        "approval",
        "file",
        "paused",
        "active_job",
    ],
)
def test_retry_requires_current_complete_proof_and_unchanged_approval(db, blocker):
    flow, aid, attempt, path = stopped(db)
    if blocker == "post_started":
        db.execute(
            "UPDATE attempts SET result=result || %s", (js({"send_started": True}),)
        )
    elif blocker == "post_observed":
        db.execute(
            "UPDATE audit SET detail=detail || %s WHERE event='greenhouse.attempt_finished'",
            (js({"post_observed": True}),),
        )
    elif blocker == "wrong_run":
        db.execute("UPDATE attempts SET result=result || %s", (js({"run_id": uid()}),))
    elif blocker == "no_stop":
        db.execute("DELETE FROM audit WHERE event='greenhouse.attempt_finished'")
    elif blocker == "approval":
        db.execute("UPDATE approvals SET invalidated=true")
    elif blocker == "file":
        path.write_bytes(b"changed file")
    elif blocker == "paused":
        flow.control(aid, "pause")
    else:
        db.execute("UPDATE jobs SET status='running' WHERE kind='submit'")
    assert not flow.submission_recovery(aid, validate=True)["can_retry"]
    with pytest.raises(ValueError):
        flow.retry_submission(aid)
    assert flow.get(aid)["state"] == "unresolved"
