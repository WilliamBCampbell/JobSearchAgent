#!/usr/bin/env python3
"""Relocate a restored state's current paths. Historical audit events stay unchanged."""

import argparse
import json
from pathlib import Path
from psycopg import sql
from jobsearch.config import Settings, atomic_write
from jobsearch.db import DB, js
from jobsearch.user_profiles import UserProfiles

p = argparse.ArgumentParser()
p.add_argument("old_root")
args = p.parse_args()
s = Settings()
db = DB(s)
db.migrate()
old = str(Path(args.old_root).expanduser().resolve())
new = str(s.state)
if old == new:
    raise SystemExit("State is already at that location")


def relocate(value):
    if isinstance(value, str):
        return value.replace(old + "/", new + "/")
    if isinstance(value, dict):
        return {k: relocate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [relocate(v) for v in value]
    return value


profiles = UserProfiles(s)
profiles.initialize()
for profile in profiles.list():
    selected = profiles.resolve(profile["id"])
    db = DB(selected)
    with db.tx() as c:
        for table, columns in [
            ("artifacts", ["path"]),
            ("tracker_snapshots", ["path"]),
            ("reports", ["path"]),
            ("llm_runs", ["directory"]),
        ]:
            for column in columns:
                c.execute(
                    sql.SQL("UPDATE {} SET {}=replace({},%s,%s)").format(
                        sql.Identifier(table),
                        sql.Identifier(column),
                        sql.Identifier(column),
                    ),
                    (old + "/", new + "/"),
                )
        for table, key, columns in [
            ("applications", "id", ["payload", "official"]),
            ("jobs", "id", ["input", "checkpoint", "result"]),
            ("receipts", "id", ["evidence"]),
            ("followups", "id", ["detail"]),
            ("hunts", "id", ["checkpoint"]),
            ("hunt_leads", "id", ["discovery", "evidence"]),
        ]:
            for row in c.execute(
                sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
            ).fetchall():
                for col in columns:
                    c.execute(
                        sql.SQL("UPDATE {} SET {}=%s WHERE {}=%s").format(
                            sql.Identifier(table),
                            sql.Identifier(col),
                            sql.Identifier(key),
                        ),
                        (js(relocate(row[col])), row[key]),
                    )
        db.audit("state.relocated", "restore", {"old_root": old, "new_root": new}, c)
    for path in (selected.fixture_workspace / "_Recovery").glob("*.json"):
        atomic_write(path, json.dumps(relocate(json.loads(path.read_text())), indent=2))
print(
    "Current state paths relocated. Run document ingestion to refresh changed artifact references before resuming work."
)
