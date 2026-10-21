#!/usr/bin/env python3
"""Back up private state and PostgreSQL. Run with services stopped."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tarfile
import json
from jobsearch.config import Settings, REPO, atomic_write

s = Settings()
p = argparse.ArgumentParser()
p.add_argument("directory", type=Path)
args = p.parse_args()
out = args.directory.expanduser().resolve()
if out.is_relative_to(REPO) or out.is_relative_to(s.state):
    raise SystemExit(
        "Choose a backup location outside the repository and private state directory"
    )
out.mkdir(parents=True, exist_ok=True, mode=0o700)
archive = out / (
    "jobsearch-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".tar.gz"
)
dump = s.state / "database-backup.dump"
with dump.open("wb") as file:
    subprocess.run(
        [
            "docker",
            "exec",
            "job-search-agent-db",
            "pg_dump",
            "-U",
            "jobsearch",
            "-d",
            "jobsearch",
            "-Fc",
        ],
        stdout=file,
        check=True,
    )
dump.chmod(0o600)
try:
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(s.state, arcname="private-state")
    archive.chmod(0o600)
finally:
    dump.unlink(missing_ok=True)
print(archive)
