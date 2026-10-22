from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from jobsearch.api import create_app
from jobsearch.config import file_hash
from jobsearch.corpus import Corpus
from jobsearch.db import DB
from jobsearch.jobspy_client import JobSpyClient
from jobsearch.memory import FactInput, Memory
from jobsearch.queue import ProfileWorker, enqueue
from jobsearch.user_profiles import ProfileInput, UserProfiles
from jobsearch.workflow import Workflow


def create_profile(profiles, tmp_path, name="Household member"):
    workspace = tmp_path / name.replace(" ", "-")
    workspace.mkdir(exist_ok=True)
    return profiles.create(ProfileInput(name=name, workspace=str(workspace)))


def put_name(db, name):
    return Memory(db).put(
        FactInput(
            candidate="real",
            key="professional_name",
            value=name,
            status="confirmed",
            provenance={"type": "user_confirmation"},
        )
    )


def test_existing_records_survive_registration_and_restart(db, tmp_path):
    fact = put_name(db, "Alex Example")
    app = Workflow(db).create("Example", "Engineer", "R-1", candidate="real")
    (db.settings.workspace / "career.txt").write_text(
        "Alex Example builds telescope software."
    )
    Corpus(db).sync("real")
    # Recreate the pre-010 extension location and rerun the upgrade with data present.
    db.execute("ALTER EXTENSION vector SET SCHEMA public")
    db.execute("DELETE FROM schema_migrations WHERE name='010_user_profiles.sql'")
    db.set_setting("llm", {"enabled": True, "backend": "codex_exec"})
    profiles = UserProfiles(db.settings)
    profiles.initialize()
    row = create_profile(profiles, tmp_path)
    profiles.initialize()
    assert len(profiles.list()) == 2
    assert (
        db.one("SELECT * FROM facts WHERE id=%s", (fact["id"],))["value"]
        == "Alex Example"
    )
    assert Workflow(db).get(app["id"])["candidate"] == "real"
    second = DB(profiles.resolve(row["id"]))
    assert second.all("SELECT * FROM facts") == []
    assert second.all("SELECT * FROM applications") == []
    assert second.settings.state == db.settings.state / "profiles" / row["id"]
    assert second.settings.token() == db.settings.token()
    assert not second.settings.live_applications
    assert not second.settings.live_tracker_writes
    assert profiles.resolve("default").workspace == db.settings.workspace
    assert (
        Corpus(db).retrieve("telescope", "real")["evidence"][0]["context"][0]["body"]
        == "Alex Example builds telescope software."
    )
    assert second.setting("llm") is None


def test_requests_settings_and_files_are_profile_scoped(db, tmp_path):
    with TestClient(create_app(db.settings)) as client:
        headers = db.settings.local_headers()
        source = tmp_path / "second-source"
        source.mkdir()
        created = client.post(
            "/api/profiles",
            headers=headers,
            json={"name": "Second search", "workspace": str(source)},
        )
        assert created.status_code == 201
        pid = created.json()["id"]
        second_headers = headers | {"X-JSA-Profile": pid}
        second = DB(UserProfiles(db.settings).resolve(pid))
        put_name(db, "Alex Example")
        put_name(second, "Sam Example")
        app = Workflow(db).create("Example", "Engineer", "R-1", candidate="real")
        other_app = Workflow(second).create(
            "Example", "Engineer", "R-1", candidate="real"
        )
        db.set_setting("official_hosts", ["one.example.invalid"])
        second.set_setting("official_hosts", ["two.example.invalid"])
        db.audit("default.only", app["id"])
        second.audit("second.only", other_app["id"])
        path = second.settings.state / "prepared-workspace" / "resume.txt"
        path.parent.mkdir(parents=True)
        path.write_text("Synthetic second profile resume")
        second.execute(
            "INSERT INTO artifacts(id,path,hash,kind) VALUES('private-file',%s,%s,'resume.txt')",
            (str(path), file_hash(path)),
        )
        assert (
            client.get("/api/artifacts/private-file", headers=headers).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/artifacts/private-file?profile={pid}",
                headers={"X-JSA-Token": db.settings.token()},
            ).text
            == path.read_text()
        )
        assert (
            client.get(
                f"/api/applications/{app['id']}", headers=second_headers
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/api/applications/{app['id']}/control",
                headers=second_headers,
                json={"action": "discard"},
            ).status_code
            == 400
        )
        assert Workflow(db).get(app["id"])["state"] == "discovered"
        assert client.get("/api/settings", headers=second_headers).json()[
            "official_hosts"
        ] == ["two.example.invalid"]
        assert client.get("/api/settings", headers=headers).json()[
            "official_hosts"
        ] == ["one.example.invalid"]
        assert not any(
            e["event"] == "default.only"
            for e in client.get("/api/audit", headers=second_headers).json()
        )

        def read_profile(i):
            selected = second_headers if i % 2 else headers
            facts = client.get("/api/facts?candidate=real", headers=selected).json()
            return facts[0]["value"]

        with ThreadPoolExecutor(max_workers=4) as pool:
            assert (
                list(pool.map(read_profile, range(12)))
                == ["Alex Example", "Sam Example"] * 6
            )
        assert (
            client.get("/api/overview", headers=second_headers).json()["profile_id"]
            == pid
        )
        assert (
            client.get(
                "/api/overview", headers=headers | {"X-JSA-Profile": "missing"}
            ).status_code
            == 404
        )
        assert (
            client.get(f"/api/overview?profile={pid}", headers=headers).status_code
            == 400
        )
        assert client.get(f"/api/overview?profile={pid}").status_code == 401


def test_profile_validation_and_missing_storage_fail_closed(db, tmp_path):
    profiles = UserProfiles(db.settings)
    profiles.initialize()
    row = create_profile(profiles, tmp_path)
    for name, folder in [
        ("Household member", tmp_path / "new"),
        ("Other", db.settings.workspace),
        ("Nested", db.settings.workspace / "nested"),
    ]:
        folder.mkdir(exist_ok=True)
        with pytest.raises(ValueError):
            profiles.create(ProfileInput(name=name, workspace=str(folder)))
    with pytest.raises(ValueError, match="already exist"):
        profiles.create(
            ProfileInput(name="Missing", workspace=str(tmp_path / "missing"))
        )
    with pytest.raises(ValueError, match="absolute"):
        profiles.create(ProfileInput(name="Relative", workspace="relative/path"))
    put_name(db, "Alex Example")
    missing = DB(replace(profiles.resolve(row["id"]), schema="missing_schema"))
    with pytest.raises(ValueError, match="storage is missing"):
        missing.all("SELECT * FROM facts")
    assert len(profiles.list()) == 2


async def test_worker_and_mcp_keep_the_queued_profile(db, tmp_path):
    profiles = UserProfiles(db.settings)
    profiles.initialize()
    row = create_profile(profiles, tmp_path)
    second = DB(profiles.resolve(row["id"]))
    (db.settings.workspace / "career.txt").write_text(
        "Alex Example specializes in orchard inventory."
    )
    (second.settings.workspace / "career.txt").write_text(
        "Sam Example specializes in telescope calibration."
    )
    first_job = enqueue(db, "ingest", {"candidate": "real"})["job_id"]
    second_job = enqueue(second, "ingest", {"candidate": "real"})["job_id"]
    worker = ProfileWorker(profiles)
    try:
        assert await worker.run_once()
        assert (
            db.one("SELECT status FROM jobs WHERE id=%s", (first_job,))["status"]
            == "complete"
        )
        assert (
            second.one("SELECT status FROM jobs WHERE id=%s", (second_job,))["status"]
            == "complete"
        )
        assert not db.one("SELECT * FROM jobs WHERE id=%s", (second_job,))
        assert all(
            "Sam Example" not in c["body"] for c in db.all("SELECT body FROM chunks")
        )
        assert all(
            "Alex Example" not in c["body"]
            for c in second.all("SELECT body FROM chunks")
        )
        # Exercise vector operators with no public table fallback in the search path.
        assert (
            Corpus(second).retrieve("telescope", "real")["evidence"][0]["context"][0][
                "body"
            ]
            == "Sam Example specializes in telescope calibration."
        )
        started = await JobSpyClient(second).call(
            "start_search", {"request": {"terms": ["engineer"], "mode": "fixture"}}
        )
        jid = started["job_id"]
        assert not db.one("SELECT * FROM jobs WHERE id=%s", (jid,))
        assert second.one("SELECT * FROM jobs WHERE id=%s", (jid,))
        # A running dispatcher discovers newly created profiles without restarting.
        third = create_profile(profiles, tmp_path, "Third search")
        third_db = DB(profiles.resolve(third["id"]))
        third_job = enqueue(third_db, "ingest", {"candidate": "real"})["job_id"]
        assert await worker.run_once()
        assert (
            third_db.one("SELECT status FROM jobs WHERE id=%s", (third_job,))["status"]
            == "complete"
        )
        result = second.one("SELECT result,status FROM jobs WHERE id=%s", (jid,))
        assert result["status"] == "complete"
        assert str(second.settings.state) in result["result"]["folder"]
    finally:
        await worker.close()


def test_state_relocation_updates_every_profile(db, tmp_path):
    import os
    import subprocess
    import sys
    from jobsearch.config import REPO

    profiles = UserProfiles(db.settings)
    profiles.initialize()
    row = create_profile(profiles, tmp_path)
    second = DB(profiles.resolve(row["id"]))
    old_root = tmp_path / "previous-state"
    for selected in (db, second):
        relative = selected.settings.state.relative_to(db.settings.state)
        selected.execute(
            "INSERT INTO artifacts(id,path,hash,kind) VALUES('restore-file',%s,'fixture-hash','resume.txt')",
            (str(old_root / relative / "prepared-workspace" / "resume.txt"),),
        )
    subprocess.run(
        [sys.executable, str(REPO / "scripts/relocate_state.py"), str(old_root)],
        env=os.environ
        | {
            "JSA_STATE": str(db.settings.state),
            "JSA_WORKSPACE": str(db.settings.workspace),
            "JSA_DATABASE_URL": db.settings.database,
        },
        check=True,
        capture_output=True,
        text=True,
    )
    for selected in (db, second):
        artifact = selected.one("SELECT path FROM artifacts WHERE id='restore-file'")
        assert artifact["path"] == str(
            selected.settings.state / "prepared-workspace" / "resume.txt"
        )
