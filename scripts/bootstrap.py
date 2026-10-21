#!/usr/bin/env python3
"""Install pinned dependencies and start the private local database."""

import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
STATE = (
    Path(os.getenv("JSA_STATE", "~/.local/share/job-search-agent"))
    .expanduser()
    .resolve()
)
IMAGE = "pgvector/pgvector@sha256:3e8b3adfd27b5707128f60956f62a793c3c9326ea8cfaf0eab7adccb5d700b21"
os.umask(0o077)
STATE.mkdir(parents=True, exist_ok=True)
url_file = STATE / "database-url"
exists = (
    subprocess.run(
        ["docker", "inspect", "job-search-agent-db"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode
    == 0
)
if exists and not url_file.exists():
    raise SystemExit(
        "The named database container exists but its private connection file is missing. Restore database-url from your backup; bootstrap will not replace an existing database."
    )
if not exists:
    if url_file.exists():
        from urllib.parse import urlsplit

        password = urlsplit(url_file.read_text().strip()).password
    else:
        password = secrets.token_hex(32)
        url_file.write_text(
            "postgresql://jobsearch:" + password + "@127.0.0.1:55432/jobsearch"
        )
        url_file.chmod(0o600)
    env = os.environ | {"POSTGRES_PASSWORD": password}
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            "job-search-agent-db",
            "--label",
            "app=job-search-agent",
            "--restart",
            "unless-stopped",
            "-p",
            "127.0.0.1:55432:5432",
            "-e",
            "POSTGRES_USER=jobsearch",
            "-e",
            "POSTGRES_PASSWORD",
            "-e",
            "POSTGRES_DB=jobsearch",
            "-v",
            "job-search-agent-pgdata:/var/lib/postgresql/data",
            IMAGE,
        ],
        env=env,
        check=True,
    )
else:
    subprocess.run(
        ["docker", "start", "job-search-agent-db"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
for _ in range(60):
    r = subprocess.run(
        ["docker", "exec", "job-search-agent-db", "pg_isready", "-U", "jobsearch"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if r.returncode == 0:
        break
    time.sleep(0.5)
else:
    raise SystemExit("Database did not become ready")
if not (ROOT / ".venv/bin/python").exists():
    subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
python = ROOT / ".venv/bin/python"
subprocess.run(
    [str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.lock")],
    check=True,
)
subprocess.run(
    [
        str(python),
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--no-build-isolation",
        "-e",
        str(ROOT),
    ],
    check=True,
)
subprocess.run(["npm", "ci"], cwd=ROOT / "web", check=True)
subprocess.run(["npm", "run", "build"], cwd=ROOT / "web", check=True)
subprocess.run([str(ROOT / ".venv/bin/jobsearch"), "migrate"], check=True)
print(
    "Ready. Run .venv/bin/python scripts/services.py start, then .venv/bin/jobsearch open."
)
