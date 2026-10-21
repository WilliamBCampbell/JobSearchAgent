"""Publication checks inspect staged blobs and history without disclosing values."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from jobsearch.config import REPO, Settings


CHECK = REPO / "scripts/check_publication.py"


@pytest.fixture
def repository(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path)

    git("init", "--quiet")
    git("config", "user.name", "Fixture contributor")
    git("config", "user.email", "contributor@example.invalid")
    return tmp_path, git


def check(path, *args):
    env = {k: v for k, v in os.environ.items() if k != "JSA_PRIVACY_DENYLIST"}
    return subprocess.run(
        [sys.executable, str(CHECK), *args],
        cwd=path,
        env=env,
        capture_output=True,
        text=True,
    )


def test_staged_private_value_is_detected_even_if_worktree_is_clean(repository):
    path, git = repository
    private = "private-fixture-contact"
    (path / ".privacy-denylist.json").write_text(json.dumps({"literals": [private]}))
    (path / "notes.md").write_text(private)
    git("add", "notes.md")
    (path / "notes.md").write_text("Clean working copy")
    result = check(path)
    assert result.returncode == 1
    assert "private-denylist-match" in result.stdout
    assert private not in result.stdout + result.stderr


def test_clean_examples_and_ignored_configuration_pass(repository):
    path, git = repository
    (path / ".gitignore").write_text(".env.local\n")
    (path / ".env.local").write_text("PRIVATE=fixture-only")
    (path / ".env.example").write_text('JSA_WORKSPACE="$HOME/job_search"\n')
    (path / "README.md").write_text("alex@example.invalid +1 202-555-0100")
    git("add", ".")
    assert check(path).returncode == 0
    assert b".env.local" not in git("ls-files")


@pytest.mark.parametrize(
    "name,data,rule",
    [
        (".env.local", b"PRIVATE=fixture-only", "private-or-generated-file"),
        ("unexpected.dat", b"\x00\xff", "binary-content"),
    ],
)
def test_forced_private_files_and_binary_data_are_rejected(
    repository, name, data, rule
):
    path, git = repository
    (path / name).write_bytes(data)
    git("add", "-f", name)
    result = check(path)
    assert result.returncode == 1 and rule in result.stdout


def test_removed_private_content_still_fails_history_scan(repository):
    path, git = repository
    private = "private-history-marker"
    (path / ".privacy-denylist.json").write_text(json.dumps({"literals": [private]}))
    (path / "notes.md").write_text(private)
    git("add", "notes.md")
    git("commit", "--quiet", "-m", "Fixture original")
    (path / "notes.md").write_text("Sanitized content")
    git("add", "notes.md")
    git("commit", "--quiet", "-m", "Fixture correction")
    assert check(path).returncode == 0
    result = check(path, "--history")
    assert result.returncode == 1 and "private-denylist-match" in result.stdout
    assert private not in result.stdout + result.stderr


def test_commit_metadata_is_scanned(repository):
    path, git = repository
    private = "Private Fixture Author"
    (path / ".privacy-denylist.json").write_text(json.dumps({"literals": [private]}))
    git("config", "user.name", private)
    git("commit", "--allow-empty", "--quiet", "-m", "Fixture commit")
    result = check(path, "--history")
    assert result.returncode == 1 and "<commit metadata>" in result.stdout
    assert private not in result.stdout + result.stderr


def test_portable_paths_preserve_defaults_and_expand_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("JSA_WORKSPACE", raising=False)
    monkeypatch.delenv("JSA_JOBSPY", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    options = dict(
        state=tmp_path / "state",
        database="fixture",
        live_applications=False,
        live_tracker_writes=False,
    )
    defaults = Settings(**options)
    assert defaults.workspace == tmp_path / "job_search"
    assert defaults.jobspy == REPO.parent / "JobSpy"
    monkeypatch.setenv("JSA_WORKSPACE", "~/custom-documents")
    monkeypatch.setenv("JSA_JOBSPY", "~/custom-adapter")
    configured = Settings(**options)
    assert configured.workspace == tmp_path / "custom-documents"
    assert configured.jobspy == tmp_path / "custom-adapter"
