import json
from pathlib import Path
import zipfile
import pytest
from jobsearch.db import uid, js
from jobsearch.config import file_hash, digest
from jobsearch.fixtures import seed, assessment, make_tracker
from jobsearch.workflow import Workflow, ApprovalInput
from jobsearch.memory import Memory, FactInput
from jobsearch.queue import Worker, enqueue
from jobsearch.tracker import Tracker, read_ods, TRACKER_NAME
from .test_scoring_search import official


def ready(db, req="NS-101", scenario="normal"):
    seed(db)
    flow = Workflow(db)
    a = flow.create("Northstar Demo", "Software Engineer, APIs", req)
    flow.set_official(a["id"], official())
    flow.score(a["id"], assessment(db))
    flow.select(a["id"])
    flow.transition(a["id"], "preparing")
    flow.transition(a["id"], "prepared")
    path = db.settings.fixture_workspace / (req + ".pdf")
    path.write_bytes(b"fixture attachment bytes")
    artifact = uid()
    fact_ids = list(db.setting("fixture_fact_ids").values())
    db.execute(
        "INSERT INTO artifacts(id,application_id,path,hash,kind,claims) VALUES(%s,%s,%s,%s,'resume.pdf',%s)",
        (artifact, a["id"], str(path), file_hash(path), js(fact_ids)),
    )
    answers = {}
    for key, value in [("salary", "120000"), ("travel", "Up to 10%")]:
        Memory(db).put(
            FactInput(
                key=key,
                value=value,
                status="confirmed",
                scope=a["id"],
                kind="answer",
                provenance={"type": "user_confirmation"},
            )
        )
        answers[key] = Memory(db).resolve(key, scope=a["id"])
    flow.update_payload(
        a["id"], {"attachments": [artifact], "answers": answers, "scenario": scenario}
    )
    flow.transition(a["id"], "filling")
    flow.transition(a["id"], "review")
    hash_ = flow.review(a["id"])["hash"]
    flow.update_payload(a["id"], {"browser_hash": hash_})
    return flow, a["id"], path


def test_submission_blocked_without_matching_authorization(db):
    flow, aid, path = ready(db)
    with pytest.raises(ValueError, match="authorization"):
        flow.begin_attempt(aid)
    with pytest.raises(ValueError, match="changed"):
        flow.authorize(aid, ApprovalInput(approve=True, reviewed_hash="a" * 64))
    assert not db.all("SELECT * FROM attempts")


def test_material_change_invalidates_approval(db):
    flow, aid, path = ready(db)
    flow.authorize(
        aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
    )
    flow.update_payload(aid, {"consents": {"background_check": True}})
    assert flow.get(aid)["state"] == "review"
    assert db.one("SELECT invalidated FROM approvals")["invalidated"]
    with pytest.raises(ValueError):
        flow.begin_attempt(aid)


def test_attachment_change_blocks_submission(db):
    flow, aid, path = ready(db)
    flow.authorize(
        aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
    )
    path.write_bytes(b"changed after review")
    with pytest.raises(ValueError, match="attachment"):
        flow.begin_attempt(aid)


def test_restart_after_attempt_never_repeats_submission(db):
    flow, aid, path = ready(db)
    job = flow.authorize(
        aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
    )
    attempt = flow.begin_attempt(aid)
    db.execute(
        "UPDATE jobs SET status='running',worker='dead',lease_until=now()-interval '1 minute' WHERE id=%s",
        (job["job_id"],),
    )
    Worker(db).recover()
    assert flow.get(aid)["state"] == "unresolved"
    assert (
        db.one("SELECT status FROM jobs WHERE id=%s", (job["job_id"],))["status"]
        == "unresolved"
    )
    with pytest.raises(ValueError):
        flow.begin_attempt(aid)
    assert len(db.all("SELECT * FROM attempts")) == 1


def test_no_receipt_is_unresolved_not_submitted(db):
    flow, aid, path = ready(db)
    flow.authorize(
        aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
    )
    flow.begin_attempt(aid)
    flow.unresolved(aid, "No receipt")
    assert flow.get(aid)["state"] == "unresolved"
    assert not db.all("SELECT * FROM receipts")


def test_tracker_totals_unrelated_rows_and_zip_members_survive(db):
    flow, aid, path = ready(db)
    flow.authorize(
        aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
    )
    attempt = flow.begin_attempt(aid)
    flow.record_receipt(
        aid,
        attempt,
        {"confirmed": True, "text": "Application received", "requisition": "NS-101"},
    )
    tracker_path = db.settings.fixture_workspace / TRACKER_NAME
    before = read_ods(tracker_path)
    with zipfile.ZipFile(tracker_path) as z:
        members = {n: z.read(n) for n in z.namelist() if n != "content.xml"}
    result = Tracker(db).update_fixture(aid, before["hash"])
    after = read_ods(tracker_path)
    assert (
        result["totals"]["tracked"] == 3
        and result["totals"]["submitted"] == 2
        and result["totals"]["prepared"] == 1
    )
    assert before["records"][:2] == after["records"][:2]
    assert (
        after["records"][1]["requisition"] == "AD-002"
        and "Deferred" in after["records"][1]["notes"]
    )
    with zipfile.ZipFile(tracker_path) as z:
        assert all(z.read(n) == v for n, v in members.items())
    assert (db.settings.fixture_workspace / "_Agent/AGENT_HANDOFF.md").exists()
    assert json.loads(Path(result["journal"]).read_text())["state"] == "committed"


def test_external_tracker_edit_blocks_update(db):
    flow, aid, path = ready(db)
    tracker_path = db.settings.fixture_workspace / TRACKER_NAME
    hash_ = file_hash(tracker_path)
    with zipfile.ZipFile(tracker_path, "a") as z:
        z.writestr("external-edit", "keep this")
    with pytest.raises(ValueError, match="externally"):
        Tracker(db).update_fixture(aid, hash_)
    assert db.one("SELECT * FROM conflicts WHERE kind='tracker_external_edit'")


def test_real_workspace_tracker_is_read_only(db):
    seed(db)
    make_tracker(db.settings.workspace / TRACKER_NAME)
    Tracker(db).import_snapshot("real")
    app = db.one("SELECT * FROM applications WHERE candidate='real' LIMIT 1")
    original = file_hash(db.settings.workspace / TRACKER_NAME)
    with pytest.raises(ValueError):
        Tracker(db).update_fixture(app["id"], original)
    with pytest.raises(ValueError):
        Workflow(db).control(app["id"], "pause")
    assert file_hash(db.settings.workspace / TRACKER_NAME) == original


def test_manual_report_threshold_is_not_repeated(db):
    for i in range(10):
        db.execute(
            "INSERT INTO followups(id,candidate,category,detail) VALUES(%s,'fixture','manual_application',%s)",
            (str(i), js({"fixture": i})),
        )
    tracker = Tracker(db)
    assert tracker.manual_report()["generated"]
    assert not tracker.manual_report()["generated"]
    assert not tracker.manual_report(on_demand=True)["generated"]


def test_cancelled_queue_job_does_not_run(db):
    job = enqueue(db, "ingest", {"candidate": "fixture"})
    db.execute(
        "UPDATE jobs SET cancel_requested=true,status='cancelled' WHERE id=%s",
        (job["job_id"],),
    )
    assert Worker(db).claim() is None


def test_fact_correction_invalidates_application_approval(db):
    flow, aid, path = ready(db)
    flow.authorize(
        aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
    )
    old = db.one(
        "SELECT * FROM facts WHERE key='salary' AND scope=%s AND active", (aid,)
    )
    Memory(db).put(
        FactInput(
            key="salary",
            value="125000",
            kind="answer",
            scope=aid,
            status="confirmed",
            supersedes=old["id"],
            provenance={"type": "user_confirmation"},
        )
    )
    assert flow.get(aid)["state"] == "review"
    assert db.one("SELECT invalidated FROM approvals")["invalidated"]
    with pytest.raises(ValueError):
        flow.begin_attempt(aid)


def test_duplicate_active_application_jobs_share_durable_id(db):
    first = enqueue(db, "fill", {"application_id": "same-application"})
    second = enqueue(db, "fill", {"application_id": "same-application"})
    assert first["job_id"] == second["job_id"]


def test_fixture_tracker_opens_in_libreoffice(db):
    import subprocess

    path = db.settings.fixture_workspace / TRACKER_NAME
    make_tracker(path)
    output = db.settings.state / "native-tracker-check"
    output.mkdir()
    subprocess.run(
        [
            "libreoffice",
            f"-env:UserInstallation=file://{db.settings.state}/lo-native-check",
            "--headless",
            "--convert-to",
            "xlsx",
            "--outdir",
            str(output),
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    converted = output / path.with_suffix(".xlsx").name
    assert converted.is_file()
    with zipfile.ZipFile(converted) as z:
        text = z.read("xl/sharedStrings.xml").decode()
        assert "AD-001" in text and "AD-002" in text and "NS-101" in text
