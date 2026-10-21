#!/usr/bin/env python3
"""Restore into a new state directory and a new database, leaving existing state intact."""

import argparse
from pathlib import Path
import os
import subprocess
import tarfile
import secrets
import psycopg
from psycopg import sql
from urllib.parse import urlsplit, urlunsplit
from jobsearch.config import Settings, REPO, atomic_write

p = argparse.ArgumentParser()
p.add_argument("archive", type=Path)
p.add_argument("destination", type=Path)
args = p.parse_args()
s = Settings()
destination = args.destination.expanduser().resolve()
if destination.exists() or destination.is_relative_to(REPO):
    raise SystemExit("Restore destination must be new and outside the repository")
destination.mkdir(mode=0o700, parents=True)
with tarfile.open(args.archive, "r:gz") as tar:
    if any(m.issym() or m.islnk() for m in tar.getmembers()):
        raise SystemExit("Restore archive contains links; review it manually")
    tar.extractall(destination, filter="data")
state = destination / "private-state"
dump = state / "database-backup.dump"
name = "jobsearch_restore_" + secrets.token_hex(4)
url = urlsplit(s.database)
admin = urlunsplit((url.scheme, url.netloc, "/postgres", url.query, url.fragment))
with psycopg.connect(admin, autocommit=True) as c:
    c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
with dump.open("rb") as f:
    subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "job-search-agent-db",
            "pg_restore",
            "-U",
            "jobsearch",
            "--no-owner",
            "--exit-on-error",
            "-d",
            name,
        ],
        stdin=f,
        check=True,
    )
new_url = urlunsplit((url.scheme, url.netloc, "/" + name, url.query, url.fragment))
atomic_write(state / "database-url", new_url)
dump.unlink()
# Local artifact paths and journal paths refer to the old state root. Restore to the original
# path for browser continuation, or keep this restore as an inspection copy.
print("Restored database and private state to " + str(state))
print(
    "Set JSA_STATE to this directory. Stored absolute artifact paths still refer to the original state; use the documented relocation command before resuming workers."
)
