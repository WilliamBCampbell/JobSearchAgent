from __future__ import annotations
import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
POLICY = "2026-10-19.1"


def digest(value: object) -> str:
    data = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, sort_keys=True, default=str).encode()
    )
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path: Path, data: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    with temp.open("xb") as f:
        os.chmod(temp, 0o600)
        f.write(data.encode() if isinstance(data, str) else data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def database_url():
    configured = os.getenv("JSA_DATABASE_URL")
    if configured:
        return configured
    path = (
        Path(os.getenv("JSA_STATE", "~/.local/share/job-search-agent")).expanduser()
        / "database-url"
    )
    if not path.exists():
        raise ValueError(
            "Run scripts/bootstrap.py to create private local database credentials"
        )
    return path.read_text().strip()


@dataclass
class Settings:
    state: Path = field(
        default_factory=lambda: Path(
            os.getenv("JSA_STATE", "~/.local/share/job-search-agent")
        )
        .expanduser()
        .resolve()
    )
    workspace: Path = field(
        default_factory=lambda: Path(
            os.getenv("JSA_WORKSPACE", str(Path.home() / "job_search"))
        )
        .expanduser()
        .resolve()
    )
    jobspy: Path = field(
        default_factory=lambda: Path(
            os.getenv("JSA_JOBSPY", str(REPO.parent / "JobSpy"))
        )
        .expanduser()
        .resolve()
    )
    database: str = field(default_factory=database_url)
    origin: str = field(
        default_factory=lambda: os.getenv("JSA_ORIGIN", "http://127.0.0.1:8765")
    )
    headless: bool = field(
        default_factory=lambda: os.getenv("JSA_HEADLESS", "0") == "1"
    )
    live_search: bool = field(
        default_factory=lambda: os.getenv("JSA_LIVE_SEARCH", "0") == "1"
    )

    live_applications: bool | None = None
    live_tracker_writes: bool | None = None

    def __post_init__(self):
        if self.state.is_relative_to(REPO):
            raise ValueError("Personal state must be outside the repository")
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state, 0o700)
        if self.live_applications is None:
            override = os.getenv("JSA_LIVE_APPLICATIONS")
            if override is not None:
                self.live_applications = override == "1"
            else:
                path = self.state / "submission-settings.json"
                config = json.loads(path.read_text()) if path.exists() else {}
                enabled = config.get("enabled", False)
                if type(enabled) is not bool:
                    raise ValueError("Submission setting 'enabled' must be a boolean")
                self.live_applications = enabled
        if self.live_tracker_writes is None:
            override = os.getenv("JSA_LIVE_TRACKER_WRITES")
            if override is not None:
                self.live_tracker_writes = override == "1"
            else:
                path = self.state / "tracker-settings.json"
                config = json.loads(path.read_text()) if path.exists() else {}
                enabled = config.get("enabled", False)
                if type(enabled) is not bool:
                    raise ValueError("Tracker setting 'enabled' must be a boolean")
                self.live_tracker_writes = enabled

    @property
    def fixture_workspace(self):
        return self.state / "fixture-workspace"

    def token(self):
        path = self.state / "api-token"
        if not path.exists():
            atomic_write(path, secrets.token_urlsafe(32))
        return path.read_text().strip()

    def source_path(self, relative: str, candidate="real"):
        root = self.workspace if candidate == "real" else self.fixture_workspace
        if candidate == "real" and relative.startswith("_AgentGenerated/"):
            root = self.state / "prepared-workspace"
            relative = relative.removeprefix("_AgentGenerated/")
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("File is outside the connected workspace or missing")
        return path

    def writable_path(self, relative: str):
        path = (self.fixture_workspace / relative).resolve()
        if not path.is_relative_to(self.fixture_workspace.resolve()):
            raise ValueError("Artifact is outside the fixture workspace")
        return path
