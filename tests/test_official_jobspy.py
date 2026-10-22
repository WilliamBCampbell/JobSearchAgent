"""Upstream MCP discovery retains the app's durable workflow contracts."""

import json
import sys
import os
import signal
import subprocess
import time
from pathlib import Path
from dataclasses import replace

import pytest
from mcp.client.stdio import StdioServerParameters

from jobsearch.config import Settings, atomic_write
from jobsearch.db import DB, uid
from jobsearch.official_jobspy import search, server_parameters
from jobsearch.search import SearchService, SearchRequest
from jobsearch.search_planning import deduplicate
from jobsearch.user_profiles import UserProfiles


@pytest.fixture
def fake_server(monkeypatch):
    monkeypatch.setattr(
        "jobsearch.official_jobspy.server_parameters",
        lambda _: StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).with_name("fake_jobspy_mcp.py"))],
        ),
    )


async def test_full_saved_records_are_not_truncated_to_mcp_previews(
    tmp_path, fake_server
):
    result = await search(
        "unused",
        {
            "search_term": "APIs",
            "site_name": ["linkedin"],
            "is_remote": True,
            "verbose": 2,
        },
        tmp_path / "full.json",
    )
    assert len(result["jobs"]) == 31
    assert all(row["description"] for row in result["jobs"])
    assert result["upstream_warnings"]["linkedin"] == [
        "is_remote isn't supported, ignoring it"
    ]


async def test_all_failed_and_partial_sources_remain_visible(tmp_path, fake_server):
    failed = await search(
        "unused",
        {"search_term": "blocked", "site_name": ["indeed"]},
        tmp_path / "failed.json",
    )
    assert not failed["jobs"]
    assert "429" in failed["messages"][0]["message"]
    assert failed["upstream_errors"]
    partial = await search(
        "unused",
        {"search_term": "partial", "site_name": ["indeed"]},
        tmp_path / "partial.json",
    )
    assert len(partial["jobs"]) == 31
    assert partial["messages"][0]["level"] == "ERROR"


async def test_bad_snapshot_count_is_rejected(tmp_path, fake_server):
    with pytest.raises(ValueError, match="count"):
        await search(
            "unused",
            {"search_term": "bad-count", "site_name": ["indeed"]},
            tmp_path / "bad.json",
        )


def test_saved_selection_environment_override_and_household_profiles(
    db, tmp_path, monkeypatch
):
    atomic_write(
        db.settings.state / "jobspy-settings.json",
        json.dumps(
            {"backend": "official_mcp", "python": str(tmp_path / "official/bin/python")}
        ),
    )
    settings = Settings(
        database=db.settings.database,
        state=db.settings.state,
        workspace=db.settings.workspace,
    )
    assert settings.jobspy_backend == "official_mcp"
    profiles = UserProfiles(settings)
    profiles.initialize()
    from .test_user_profiles import create_profile

    profile = create_profile(profiles, tmp_path)
    second = profiles.resolve(profile["id"])
    assert second.jobspy_backend == "official_mcp"
    assert second.jobspy_mcp_python == settings.jobspy_mcp_python
    assert DB(second).all("SELECT * FROM applications") == []
    monkeypatch.setenv("JSA_JOBSPY_BACKEND", "legacy")
    assert (
        Settings(database=db.settings.database, state=db.settings.state).jobspy_backend
        == "legacy"
    )


def test_official_worker_contract_manifests_resume_and_no_legacy_checkout(
    db, tmp_path, monkeypatch
):
    settings = replace(
        db.settings,
        jobspy=tmp_path / "absent-checkout",
        jobspy_backend="official_mcp",
        jobspy_mcp_python=Path(sys.executable),
    )
    service = SearchService(DB(settings))
    import jobsearch.search as module

    popen = module.subprocess.Popen
    commands = []

    def fake_worker(command, **kwargs):
        commands.append(command)
        assert command[1:] == ["-m", "jobsearch.official_jobspy"]
        return popen(
            [
                sys.executable,
                str(Path(__file__).with_name("fake_jobspy_mcp.py")),
                "--worker",
            ],
            **kwargs,
        )

    monkeypatch.setattr(module.subprocess, "Popen", fake_worker)
    job = {
        "id": uid(),
        "input": SearchRequest(
            mode="live", terms=["API engineer"], sites=["linkedin"]
        ).model_dump(),
    }
    checkpoints = []
    result = service.run(job, checkpoints.append, lambda: False)
    manifest = result["manifest"]
    entry = manifest["queries"][0]
    assert manifest["backend"] == "official_mcp"
    assert entry["requested_filters"]["is_remote"] is True
    assert "is_remote" not in entry["effective_filters"]
    assert entry["upstream_warnings"]["linkedin"]
    assert entry["status"] == "ok" and entry["count"] == 1
    assert len(commands) == 1
    assert (Path(result["folder"]) / "jobs.json").is_file()
    service.run(job, checkpoints.append, lambda: False)
    assert len(commands) == 1  # Completed queries survive an application restart.


def test_fixture_search_never_starts_official_mcp(db, monkeypatch):
    service = SearchService(
        DB(
            replace(
                db.settings,
                jobspy_backend="official_mcp",
                jobspy=Path("/nonexistent-fixture-adapter"),
            )
        )
    )
    monkeypatch.setattr(
        "jobsearch.search.subprocess.Popen",
        lambda *a, **kw: pytest.fail("Fixture search must stay offline"),
    )
    result = service.run(
        {"id": uid(), "input": SearchRequest(terms=["API engineer"]).model_dump()},
        lambda _: None,
        lambda: False,
    )
    assert result["manifest"]["backend"] == "fixture"
    assert result["manifest"]["raw_count"] > 0


def test_dedup_keeps_query_provenance_and_recovers_missing_details():
    first = {
        "site": "indeed",
        "id": "1",
        "search_term": "APIs",
        "description": None,
        "is_remote": False,
    }
    rows = deduplicate(
        [
            first,
            first
            | {"search_term": ".NET", "description": "Full posting", "is_remote": True},
        ]
    )
    assert len(rows) == 1
    assert rows[0]["search_terms"] == ["APIs", ".NET"]
    assert rows[0]["description"] == "Full posting"
    assert rows[0]["is_remote"] is False


def test_official_server_receives_proxy_configuration_without_app_credentials(
    monkeypatch,
):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("JSA_DATABASE_URL", "synthetic-private-database-setting")
    parameters = server_parameters(sys.executable)
    assert parameters.env["HTTPS_PROXY"] == "http://proxy.example:8080"
    assert "JSA_DATABASE_URL" not in parameters.env


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-lifetime contract")
def test_scraper_ends_when_worker_is_terminated(tmp_path):
    marker = tmp_path / "scraper.pid"
    code = server_parameters(sys.executable).args[1]
    # Supply installed-version metadata and a sleeping scraper. Keep the actual
    # production parent-death guard and the SDK's separate-session arrangement.
    code = (
        "from importlib import metadata\noriginal = metadata.version\nmetadata.version = lambda name: {'python-jobspy':'1.4.0','mcp-jobspy':'1.4.0','mcp':'2.3.0'}.get(name) or original(name)\n"
        + code.replace(
            "from jobspy.mcp import main\nmain()",
            f"from pathlib import Path\nimport time\nPath({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(60)",
        )
    )
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]],start_new_session=True); time.sleep(60)",
            code,
        ],
        start_new_session=True,
    )
    child = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        child = int(marker.read_text())
        parent.terminate()
        parent.wait(timeout=3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{child}/stat")
            if not stat.exists() or stat.read_text().split()[2] == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("Upstream scraper survived its worker")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
