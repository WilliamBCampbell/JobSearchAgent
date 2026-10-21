"""Reviewed writes use the same engine for isolated real-mode and fixture workspaces."""

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lxml import etree as E

from jobsearch.api import create_app
from jobsearch.config import Settings, atomic_write, file_hash
from jobsearch.db import js
from jobsearch.fixtures import seed, make_tracker
from jobsearch.tracker import Tracker, TRACKER_NAME, read_ods, cells, cell_text, NS
from jobsearch.tracker_write import TrackerWriter, set_cell
from jobsearch.tracker_metrics import dashboard
from jobsearch.workflow import Workflow, ApprovalInput
from .test_workflow_tracker import ready


def prepared(db, req="NEW-2", real=False):
    seed(db)
    aid = Workflow(db).create("Northstar Demo", "Software Engineer, APIs", req)["id"]
    db.execute(
        "UPDATE applications SET state='prepared',candidate=%s WHERE id=%s",
        ("real" if real else "fixture", aid),
    )
    if real:
        make_tracker(db.settings.workspace / TRACKER_NAME)
    return aid


def save(db, aid):
    plan = Tracker(db).preview(aid)
    return Tracker(db).update(aid, plan["expected_hash"], plan["review_hash"])


def edit_cell(path, req, column, value):
    with zipfile.ZipFile(path) as z:
        members = {i.filename: (i, z.read(i.filename)) for i in z.infolist()}
    tree = E.fromstring(members["content.xml"][1])
    row = next(
        r
        for r in tree.findall(".//t:table-row", NS)
        if len(cells(r)) >= 8 and req in cell_text(cells(r)[1])
    )
    set_cell(cells(row)[column], value)
    with zipfile.ZipFile(path, "w") as z:
        for name, (info, data) in members.items():
            z.writestr(info, E.tostring(tree) if name == "content.xml" else data)


def test_real_mode_default_and_persistence(tmp_path, monkeypatch):
    monkeypatch.delenv("JSA_LIVE_TRACKER_WRITES", raising=False)
    args = {"state": tmp_path / "private", "database": "unused"}
    assert not Settings(**args).live_tracker_writes
    path = args["state"] / "tracker-settings.json"
    atomic_write(path, '{"enabled": true}')
    assert Settings(**args).live_tracker_writes
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("JSA_LIVE_TRACKER_WRITES", "0")
    assert not Settings(**args).live_tracker_writes
    monkeypatch.delenv("JSA_LIVE_TRACKER_WRITES")
    atomic_write(path, '{"enabled": "false"}')
    with pytest.raises(ValueError, match="boolean"):
        Settings(**args)


def test_real_write_enable_is_separate_from_review_and_preserves_other_rows(
    db, monkeypatch
):
    monkeypatch.delenv("JSA_LIVE_TRACKER_WRITES", raising=False)
    aid = prepared(db, real=True)
    path = db.settings.workspace / TRACKER_NAME
    before = read_ods(path)
    plan = Tracker(db).preview(aid)
    with pytest.raises(ValueError, match="disabled"):
        save(db, aid)
    with TestClient(create_app(db.settings)) as client:
        headers = {"X-JSA-Token": db.settings.token()}
        assert (
            client.put(
                "/api/settings/tracker-writes", json={"enabled": True}
            ).status_code
            == 401
        )
        response = client.put(
            "/api/settings/tracker-writes", json={"enabled": True}, headers=headers
        )
        assert response.status_code == 200
        assert file_hash(path) == before["hash"]
        assert not db.all("SELECT * FROM jobs")
        assert (
            client.post(
                "/api/tracker/update/" + aid,
                json={"expected_hash": before["hash"]},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/tracker/update/" + aid,
                json={
                    "expected_hash": before["hash"],
                    "reviewed_hash": plan["review_hash"],
                },
                headers=headers,
            ).status_code
            == 400
        )
        fresh = client.get("/api/tracker/preview/" + aid, headers=headers).json()
        result = client.post(
            "/api/tracker/update/" + aid,
            json={
                "expected_hash": fresh["expected_hash"],
                "reviewed_hash": fresh["review_hash"],
            },
            headers=headers,
        )
        assert result.status_code == 200, result.text
    after = read_ods(path)
    assert after["records"][:3] == before["records"]
    assert after["totals"]["tracked"] == 4
    assert after["records"][-1]["requisition"] == "NEW-2"
    journal = json.loads(Path(result.json()["journal"]).read_text())
    assert file_hash(Path(journal["backup"])) == before["hash"]
    assert journal["state"] == "committed"
    save(db, aid)
    assert read_ods(path)["totals"]["tracked"] == 4
    assert Settings(
        state=db.settings.state, database=db.settings.database
    ).live_tracker_writes


def test_exact_requisition_and_alphabetic_ids(db):
    aid = prepared(db, req="101")
    save(db, aid)
    path = db.settings.fixture_workspace / TRACKER_NAME
    assert read_ods(path)["totals"]["tracked"] == 4  # NS-101 is distinct
    other = Workflow(db).create("Northstar Demo", "Engineer", "ALPHA")["id"]
    db.execute("UPDATE applications SET state='deferred' WHERE id=%s", (other,))
    save(db, other)
    save(db, other)
    snapshot = read_ods(path)
    assert snapshot["totals"]["tracked"] == 5
    assert snapshot["records"][-1]["requisition"] == "ALPHA"
    assert "Deferred" in snapshot["records"][1]["notes"]


def test_existing_public_id_submission_is_noop_preserves_history(db):
    flow, aid, _ = ready(db)
    flow.authorize(
        aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
    )
    attempt = flow.begin_attempt(aid)
    flow.record_receipt(
        aid,
        attempt,
        {"confirmed": True, "text": "Application received", "requisition": "NS-101"},
    )
    path = db.settings.fixture_workspace / TRACKER_NAME
    edit_cell(path, "NS-101", 1, "Software Engineer (1234567)")
    edit_cell(path, "1234567", 3, "Submitted")
    db.execute(
        "UPDATE applications SET employer='Northstar Demo Inc.',requisition='3',official=%s WHERE id=%s",
        (
            js(
                {
                    "application_url": "https://job-boards.greenhouse.io/demo/jobs/1234567"
                }
            ),
            aid,
        ),
    )
    before = path.read_bytes()
    result = save(db, aid)
    assert result["action"] == "already_recorded"
    assert path.read_bytes() == before
    assert not (db.settings.fixture_workspace / "_Backups").exists()


def test_receipt_missing_and_changed_review_block_write(db):
    aid = prepared(db)
    path = db.settings.fixture_workspace / TRACKER_NAME
    before = file_hash(path)
    plan = Tracker(db).preview(aid)
    db.execute(
        "UPDATE applications SET state='submitted',revision=revision+1 WHERE id=%s",
        (aid,),
    )
    with pytest.raises(ValueError, match="receipt"):
        Tracker(db).update(aid, before, plan["review_hash"])
    db.execute("UPDATE applications SET state='deferred' WHERE id=%s", (aid,))
    with pytest.raises(ValueError, match="preview changed"):
        Tracker(db).update(aid, before, plan["review_hash"])
    assert file_hash(path) == before


@pytest.mark.parametrize("external", [False, True])
def test_partial_save_recovery_preserves_external_edits(db, monkeypatch, external):
    aid = prepared(db)
    import jobsearch.tracker_write as module

    original = module.atomic_write

    def interrupted(path, content):
        if str(path).endswith("_Agent/AGENT_HANDOFF.md"):
            raise OSError("synthetic interrupted sidecar")
        return original(path, content)

    monkeypatch.setattr(module, "atomic_write", interrupted)
    with pytest.raises(ValueError, match="need recovery"):
        save(db, aid)
    root = db.settings.fixture_workspace
    assert read_ods(root / TRACKER_NAME)["totals"]["tracked"] == 4
    monkeypatch.setattr(module, "atomic_write", original)
    if external:
        original(root / "_Agent/AGENT_HANDOFF.md", "External correction")
        assert not Tracker(db).recover()[0]["recovered"]
        assert (root / "_Agent/AGENT_HANDOFF.md").read_text() == "External correction"
    else:
        assert Tracker(db).recover()[0]["recovered"]
        assert "AD-002" in (root / "_Agent/AGENT_HANDOFF.md").read_text()
        assert not Tracker(db).recover()


def test_recovery_rejects_edited_ods_and_outside_paths(db, monkeypatch):
    aid = prepared(db)
    monkeypatch.setattr(
        TrackerWriter,
        "finish",
        lambda *a: (_ for _ in ()).throw(OSError("interrupted")),
    )
    with pytest.raises(ValueError):
        save(db, aid)
    monkeypatch.undo()
    path = db.settings.fixture_workspace / TRACKER_NAME
    edit_cell(path, "AD-002", 7, "Keep my external correction")
    current = file_hash(path)
    result = Tracker(db).recover()
    assert not result[0]["recovered"]
    assert "changed after replacement" in result[0]["reason"]
    assert file_hash(path) == current
    with pytest.raises(ValueError, match="outside"):
        TrackerWriter(db).path(db.settings.workspace, "../private.txt")


def test_metrics_are_current_and_do_not_double_count_imports(db):
    aid = prepared(db)
    Tracker(db).import_snapshot("fixture")
    result = dashboard(db, "fixture")
    assert result["tracker"]["totals"]["tracked"] == 3
    assert result["workflow_counts"] == {"prepared": 1}
    assert [a["id"] for a in result["applications"]] == [aid]
    path = db.settings.fixture_workspace / TRACKER_NAME
    edit_cell(path, "AD-001", 3, "Rejected")
    edit_cell(path, "AD-001", 2, "10/14/26")
    current = dashboard(db, "fixture")
    assert current["tracker"]["totals"]["rejected"] == 1
    assert current["monthly_applications"]["2026-10"] >= 1
    assert result["tracker"]["hash"] != current["tracker"]["hash"]


def test_reimport_after_sync_preserves_runtime_identity_and_receipt_gate(db):
    aid = prepared(db)
    save(db, aid)
    assert not Tracker(db).import_snapshot("fixture")["conflicts"]
    assert (
        db.one("SELECT count(*) AS n FROM applications WHERE requisition='NEW-2'")["n"]
        == 1
    )
    path = db.settings.fixture_workspace / TRACKER_NAME
    edit_cell(path, "NEW-2", 3, "Submitted")
    result = Tracker(db).import_snapshot("fixture")
    assert len(result["conflicts"]) == 1
    Tracker(db).resolve_import_conflict(result["conflicts"][0], "ods")
    assert Workflow(db).get(aid)["state"] == "prepared"
    assert not db.all("SELECT * FROM receipts WHERE application_id=%s", (aid,))
    with pytest.raises(ValueError, match="earlier workflow"):
        Tracker(db).preview(aid)


def test_external_edit_acknowledgment_requires_fresh_preview(db):
    aid = prepared(db)
    plan = Tracker(db).preview(aid)
    path = db.settings.fixture_workspace / TRACKER_NAME
    edit_cell(path, "AD-002", 7, "Keep my deferred note")
    with pytest.raises(ValueError, match="externally"):
        Tracker(db).update(aid, plan["expected_hash"], plan["review_hash"])
    conflict = db.one("SELECT id FROM conflicts WHERE NOT resolved")
    Tracker(db).resolve_import_conflict(conflict["id"], "ods")
    with pytest.raises(ValueError, match="preview changed"):
        Tracker(db).update(aid, file_hash(path), plan["review_hash"])
    save(db, aid)
    assert read_ods(path)["records"][1]["notes"] == "Keep my deferred note"


def test_preview_totals_and_disabled_mode_cannot_write_after_review(db):
    aid = prepared(db, real=True)
    db.settings.live_tracker_writes = True
    plan = Tracker(db).preview(aid)
    assert plan["totals_before"]["tracked"] == 3
    assert plan["totals_after"]["tracked"] == 4
    db.settings.live_tracker_writes = False
    with pytest.raises(ValueError, match="disabled"):
        Tracker(db).update(aid, plan["expected_hash"], plan["review_hash"])
    assert file_hash(db.settings.workspace / TRACKER_NAME) == plan["expected_hash"]


def test_tracker_setting_environment_override_and_strict_boolean(db, monkeypatch):
    with TestClient(create_app(db.settings)) as client:
        headers = {"X-JSA-Token": db.settings.token()}
        assert (
            client.put(
                "/api/settings/tracker-writes",
                json={"enabled": "true"},
                headers=headers,
            ).status_code
            == 422
        )
        monkeypatch.setenv("JSA_LIVE_TRACKER_WRITES", "0")
        assert (
            client.put(
                "/api/settings/tracker-writes", json={"enabled": True}, headers=headers
            ).status_code
            == 400
        )
        assert not (db.settings.state / "tracker-settings.json").exists()


def test_updated_tracker_opens_in_libreoffice(db):
    import subprocess

    aid = prepared(db)
    save(db, aid)
    path = db.settings.fixture_workspace / TRACKER_NAME
    output = db.settings.state / "updated-tracker-check"
    output.mkdir()
    subprocess.run(
        [
            "libreoffice",
            f"-env:UserInstallation=file://{db.settings.state}/lo-updated-check",
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
    with zipfile.ZipFile(output / path.with_suffix(".xlsx").name) as z:
        text = z.read("xl/sharedStrings.xml").decode()
        assert "NEW-2" in text and "AD-002" in text
