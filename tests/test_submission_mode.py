import json

import pytest
from fastapi.testclient import TestClient

from jobsearch.api import create_app
from jobsearch.cli import main
from jobsearch.config import Settings, atomic_write
from jobsearch.workflow import ApprovalInput
from .test_workflow_tracker import ready


def test_submission_mode_is_private_persistent_and_environment_overridable(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("JSA_LIVE_APPLICATIONS", raising=False)
    args = {"state": tmp_path / "private", "database": "unused"}
    assert Settings(**args).live_applications is False
    path = args["state"] / "submission-settings.json"
    atomic_write(path, json.dumps({"enabled": True}))
    assert Settings(**args).live_applications is True
    assert path.stat().st_mode & 0o777 == 0o600
    assert Settings(**args, live_applications=False).live_applications is False
    monkeypatch.setenv("JSA_LIVE_APPLICATIONS", "0")
    assert Settings(**args).live_applications is False
    monkeypatch.delenv("JSA_LIVE_APPLICATIONS")
    atomic_write(path, '{"enabled": "false"}')
    with pytest.raises(ValueError, match="boolean"):
        Settings(**args)


def run_mode(db, monkeypatch, mode):
    monkeypatch.delenv("JSA_LIVE_APPLICATIONS", raising=False)
    monkeypatch.setattr("jobsearch.cli.Settings", lambda: db.settings)
    monkeypatch.setattr("sys.argv", ["jobsearch", "submissions", "--mode", mode])
    main()


def test_enabling_sending_does_not_authorize_or_queue_an_application(db, monkeypatch):
    flow, aid, _ = ready(db)
    db.execute("UPDATE applications SET candidate='real' WHERE id=%s", (aid,))
    review_hash = flow.get(aid)["payload"]["browser_hash"]
    with pytest.raises(ValueError, match="disabled"):
        flow.authorize(aid, ApprovalInput(approve=True, reviewed_hash=review_hash))
    run_mode(db, monkeypatch, "enabled")
    restarted = Settings(state=db.settings.state, database=db.settings.database)
    assert restarted.live_applications
    assert flow.get(aid)["state"] == "review"
    assert not db.all("SELECT * FROM approvals")
    assert not db.all("SELECT * FROM jobs WHERE kind='submit'")
    assert not db.all("SELECT * FROM attempts")
    with TestClient(create_app(restarted)) as client:
        assert client.get("/health").json()["real_submission_enabled"] is True
        assert (
            client.get(
                "/api/settings", headers={"X-JSA-Token": restarted.token()}
            ).json()["real_submission_enabled"]
            is True
        )
    run_mode(db, monkeypatch, "disabled")
    assert not Settings(
        state=db.settings.state, database=db.settings.database
    ).live_applications


def test_saved_mode_cannot_silently_override_environment(db, monkeypatch):
    monkeypatch.setenv("JSA_LIVE_APPLICATIONS", "0")
    monkeypatch.setattr("jobsearch.cli.Settings", lambda: db.settings)
    monkeypatch.setattr("sys.argv", ["jobsearch", "submissions", "--mode", "enabled"])
    with pytest.raises(SystemExit, match="environment overrides"):
        main()
    assert not (db.settings.state / "submission-settings.json").exists()


@pytest.mark.parametrize("pending", ["approval", "job", "attempt"])
def test_enabling_cannot_resume_old_real_authorizations(db, monkeypatch, pending):
    flow, aid, _ = ready(db)
    flow.authorize(
        aid, ApprovalInput(approve=True, reviewed_hash=flow.review(aid)["hash"])
    )
    if pending == "attempt":
        flow.begin_attempt(aid)
    elif pending == "job":
        db.execute("UPDATE approvals SET invalidated=true")
        db.execute("UPDATE applications SET state='review' WHERE id=%s", (aid,))
    else:
        db.execute("DELETE FROM jobs")
        db.execute("UPDATE applications SET state='review' WHERE id=%s", (aid,))
    db.execute("UPDATE applications SET candidate='real' WHERE id=%s", (aid,))
    with pytest.raises(SystemExit, match="Resolve existing"):
        run_mode(db, monkeypatch, "enabled")
    assert not (db.settings.state / "submission-settings.json").exists()
