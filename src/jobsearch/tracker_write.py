"""Reviewed ODS updates shared by fixture and explicitly enabled real workspaces."""

from __future__ import annotations

import copy
import fcntl
import io
import json
import re
import html
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zipfile import ZipFile

from lxml import etree as E

from .config import atomic_write, digest, file_hash
from .db import js, uid
from .tracker import (
    NS,
    T,
    X,
    O,
    TRACKER_NAME,
    ACTIVE,
    normalize,
    cells,
    cell_text,
    read_ods,
    totals_for,
)


def employer_key(value):
    parts = normalize(value).split()
    while parts and parts[-1] in {
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
    }:
        parts.pop()
    return " ".join(parts)


def set_cell(cell, value):
    for key in list(cell.attrib):
        if key.startswith(O) or key == T + "formula":
            del cell.attrib[key]
    for child in list(cell):
        cell.remove(child)
    cell.set(O + "value-type", "string")
    E.SubElement(cell, X + "p").text = str(value)


class TrackerWriter:
    def __init__(self, db):
        self.db = db

    def root(self, candidate):
        if candidate not in ("real", "fixture"):
            raise ValueError("Unknown workspace")
        return (
            self.db.settings.workspace
            if candidate == "real"
            else self.db.settings.fixture_workspace
        )

    def allowed(self, candidate):
        if candidate == "real" and not self.db.settings.live_tracker_writes:
            raise ValueError(
                "Real tracker writes are disabled. Enable tracker writes in Settings first."
            )

    def path(self, root, relative):
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
            raise ValueError("Tracker file is outside the configured workspace")
        return path

    def application(self, aid):
        app = self.db.one("SELECT * FROM applications WHERE id=%s", (aid,))
        if not app:
            raise ValueError("Application not found")
        if app["imported"]:
            raise ValueError(
                "Imported tracker records remain authoritative; resolve their import conflicts instead."
            )
        if self.db.one(
            "SELECT 1 FROM conflicts WHERE subject=%s AND NOT resolved", (aid,)
        ):
            raise ValueError(
                "Resolve this application's tracker conflicts before syncing"
            )
        return app

    def identifiers(self, app):
        ids = {app["requisition"]}
        public_id = None
        url = urlparse(app["official"].get("application_url", ""))
        parts = url.path.strip("/").split("/")
        if (
            url.hostname in ("job-boards.greenhouse.io", "boards.greenhouse.io")
            and len(parts) == 3
            and parts[1] == "jobs"
            and parts[2].isdigit()
        ):
            public_id = parts[2]
            ids.add(public_id)
        return ids, public_id or app["requisition"]

    def prepare(self, aid):
        app = self.application(aid)
        root = self.root(app["candidate"])
        path = self.path(root, TRACKER_NAME)
        if not path.exists():
            raise ValueError("No tracker found in this workspace")
        snapshot = read_ods(path)
        ids, display_id = self.identifiers(app)
        status = {
            "submitted": "Submitted",
            "unresolved": "Unresolved submission",
            "deferred": "Prepared, not submitted",
            "prepared": "Prepared, not submitted",
            "closed": "Closed without submission",
            "skipped": "Deprioritized",
        }.get(app["state"])
        if not status:
            raise ValueError("Application is not in a tracker-ready state")
        receipts = self.db.all(
            "SELECT * FROM receipts WHERE application_id=%s ORDER BY created_at,id",
            (aid,),
        )
        if app["state"] == "submitted" and not receipts:
            raise ValueError("A receipt is required before tracker submission status")
        matches = [
            row
            for row in snapshot["records"]
            if employer_key(row["company"]) == employer_key(app["employer"])
            and row["requisition"] in ids
        ]
        ambiguous = [
            row
            for row in snapshot["records"]
            if employer_key(row["company"]) == employer_key(app["employer"])
            and not row["requisition"]
            and normalize(row["role"]) == normalize(app["role"])
        ]
        if len(matches) > 1 or ambiguous:
            raise ValueError(
                "Tracker identity is ambiguous; review employer, role, and requisition before syncing"
            )
        before = matches[0] if matches else None
        if before and before["status"] in ACTIVE and status == "Submitted":
            action = "already_recorded"
        elif before and before["status"] == "Rejected":
            raise ValueError(
                "The authoritative tracker records a rejection. Resolve the status conflict before writing."
            )
        elif before and before["status"] in ACTIVE and status != "Submitted":
            raise ValueError(
                "Tracker sync cannot replace a submitted application with an earlier workflow state"
            )
        else:
            action = "update" if before else "insert"
        now = datetime.now(timezone.utc).date().isoformat()
        row = (
            list(before["raw"][:8])
            if before
            else [
                app["employer"],
                app["role"]
                + " ("
                + (
                    display_id
                    if any(c.isdigit() for c in display_id)
                    else "Requisition: " + display_id
                )
                + ")",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )
        row += [""] * (8 - len(row))
        if action != "already_recorded":
            row[3], row[4] = status, now
            if status == "Submitted":
                row[2] = receipts[0]["created_at"].date().isoformat()
                # Use only files frozen for the receipted attempt and actually uploaded.
                receipt = receipts[-1]
                attempt = (
                    self.db.one(
                        "SELECT * FROM attempts WHERE id=%s AND application_id=%s",
                        (receipt["attempt_id"], aid),
                    )
                    if receipt["attempt_id"]
                    else None
                )
                attached = {}
                if attempt:
                    frozen = (
                        self.db.settings.state
                        / "submitted-payloads"
                        / (attempt["id"] + ".json")
                    )
                    payload = json.loads(frozen.read_text())
                    if digest(payload) != attempt["payload_hash"]:
                        raise ValueError(
                            "Submitted document record does not match the approved payload"
                        )
                    for item in payload.get("attachments", []):
                        artifact = self.db.one(
                            "SELECT * FROM artifacts WHERE id=%s AND application_id=%s AND attached AND immutable",
                            (item["id"], aid),
                        )
                        if not artifact or artifact["hash"] != item["hash"]:
                            raise ValueError(
                                "Submitted attachment evidence needs reconciliation"
                            )
                        attached[item["kind"]] = artifact["path"]
                row[5] = attached.get("resume.pdf", "Not recorded")
                row[6] = attached.get(
                    "cover_letter.pdf", "No" if attempt else "Not recorded"
                )
            marker = f"Agent record {aid}: {app['state']}."
            if marker not in row[7]:
                row[7] = (row[7].rstrip() + "\n" + marker).strip()
        sidecars = {}
        if action != "already_recorded":
            for rel in (
                f"_Agent/Application_Notes/{aid}.md",
                "_Agent/AGENT_HANDOFF.md",
                "_Agent/Application_Checklist.html",
            ):
                target = self.path(root, rel)
                sidecars[rel] = file_hash(target) if target.exists() else None
        proposed_records = [dict(r) for r in snapshot["records"]]
        if action == "insert":
            proposed_records.append({"status": status})
        elif action == "update":
            for record in proposed_records:
                if (
                    record["sheet"] == before["sheet"]
                    and record["row"] == before["row"]
                ):
                    record["status"] = status
        plan = {
            "application_id": aid,
            "candidate": app["candidate"],
            "tracker_path": str(path),
            "expected_hash": snapshot["hash"],
            "action": action,
            "before": (before["raw"] + [""] * 8)[:8] if before else None,
            "after": row,
            "sheet": before["sheet"] if before else None,
            "row": before["row"] if before else None,
            "sidecars": sidecars,
            "totals_before": snapshot["totals"],
            "totals_after": totals_for(proposed_records),
            "application_revision": app["revision"],
            "state": app["state"],
            "receipt_ids": [r["id"] for r in receipts],
            "reason": "This opening is already recorded as submitted. Its date, documents, and notes will be preserved."
            if action == "already_recorded"
            else "The preview includes the application row, summary totals, and generated notes, checklist, and handoff.",
            "writes_enabled": app["candidate"] == "fixture"
            or self.db.settings.live_tracker_writes,
        }
        plan["review_hash"] = digest(plan)
        return plan, app, snapshot

    def archive(self, plan):
        path = Path(plan["tracker_path"])
        with ZipFile(path) as archive:
            infos = archive.infolist()
            members = {i.filename: archive.read(i.filename) for i in infos}
        tree = E.fromstring(
            members["content.xml"],
            parser=E.XMLParser(resolve_entities=False, no_network=True),
        )
        tables = []
        for table in tree.findall(".//t:table", NS):
            rows = table.findall("t:table-row", NS)
            header = next(
                (
                    i
                    for i, r in enumerate(rows)
                    if len(cells(r)) >= 4
                    and cell_text(cells(r)[0]).lower() == "company"
                    and cell_text(cells(r)[3]).lower() == "status"
                ),
                None,
            )
            if header is not None:
                tables.append((table, rows, header))
        target = (
            [t for t in tables if t[0].get(T + "name") == plan["sheet"]]
            if plan["sheet"]
            else tables
        )
        if len(target) != 1:
            raise ValueError(
                "Choose a single applications sheet before inserting a tracker row"
            )
        table, rows, header = target[0]
        before_xml = {r: E.tostring(r) for r in tree.findall(".//t:table-row", NS)}
        if any(
            int(r.get(T + "number-rows-repeated", "1")) != 1
            and any(cell_text(c) for c in cells(r))
            for r in rows
        ):
            raise ValueError(
                "Repeated populated rows need manual tracker reconciliation"
            )
        if plan["action"] == "update":
            matches = [
                r
                for r in rows[header + 1 :]
                if ([cell_text(c) for c in cells(r)] + [""] * 8)[:8] == plan["before"]
            ]
            if len(matches) != 1:
                raise ValueError(
                    "Tracker row changed or uses unsupported repeated cells"
                )
            row = matches[0]
        else:
            template = next(
                (
                    r
                    for r in reversed(rows[header + 1 :])
                    if len(cells(r)) == 8 and cell_text(cells(r)[0])
                ),
                None,
            )
            row = (
                copy.deepcopy(template)
                if template is not None
                else E.Element(T + "table-row")
            )
            if template is None:
                for _ in range(8):
                    E.SubElement(row, T + "table-cell")
            table.insert(
                table.index(template) + 1
                if template is not None
                else table.index(rows[header]) + 1,
                row,
            )
        if len(cells(row)) != 8 or any(
            any(
                c.get(T + k, "1") != "1"
                for k in (
                    "number-columns-repeated",
                    "number-columns-spanned",
                    "number-rows-spanned",
                )
            )
            for c in cells(row)
        ):
            raise ValueError(
                "The application row uses unsupported merged or repeated cells"
            )
        for cell, value in zip(cells(row), plan["after"]):
            # Preserve hyperlinks and other content in unchanged cells.
            if cell_text(cell) != value:
                set_cell(cell, value)
        changed = {row}

        def encode():
            updated = dict(members)
            updated["content.xml"] = E.tostring(
                tree, encoding="utf-8", xml_declaration=True
            )
            stream = io.BytesIO()
            with ZipFile(stream, "w") as archive:
                archive.writestr("mimetype", updated["mimetype"], compress_type=0)
                for info in infos:
                    if info.filename != "mimetype":
                        archive.writestr(copy.copy(info), updated[info.filename])
            return stream.getvalue()

        # read_ods accepts an in-memory ZIP, preserving the same counting rules.
        totals = read_ods(io.BytesIO(encode()))["totals"]
        for _, table_rows, table_header in tables:
            for summary in table_rows[:table_header]:
                cs = cells(summary)
                for i, cell in enumerate(cs[:-1]):
                    label = cell_text(cell).strip().lower()
                    if label in totals or label == "last updated":
                        set_cell(
                            cs[i + 1],
                            totals[label] if label in totals else plan["after"][4],
                        )
                        changed.add(summary)
        if any(
            E.tostring(r) != original
            for r, original in before_xml.items()
            if r not in changed
        ):
            raise ValueError("An unrelated tracker row changed")
        content = encode()
        with ZipFile(io.BytesIO(content)) as archive:
            if archive.testzip() or any(
                archive.read(n) != v for n, v in members.items() if n != "content.xml"
            ):
                raise ValueError("Tracker archive validation failed")
        return content, read_ods(io.BytesIO(content))

    def sidecar_content(self, plan, snapshot):
        rows = snapshot["records"]
        summary = json.dumps(snapshot["totals"], sort_keys=True)
        return {
            f"_Agent/Application_Notes/{plan['application_id']}.md": "# Application record\n\n"
            + "\n".join(
                f"{label}: {value}"
                for label, value in zip(
                    (
                        "Company",
                        "Role",
                        "Date applied",
                        "Status",
                        "Last update",
                        "Resume",
                        "Cover letter",
                        "Notes",
                    ),
                    plan["after"],
                )
            )
            + "\n\nTracker totals: "
            + summary
            + "\n",
            "_Agent/AGENT_HANDOFF.md": "# Application handoff\n\nGenerated from the current tracker.\n\n"
            + "\n".join(f"- {r['company']} | {r['role']} | {r['status']}" for r in rows)
            + "\n\nTracker totals: "
            + summary
            + "\n",
            "_Agent/Application_Checklist.html": '<!doctype html><meta charset="utf-8"><h1>Application checklist</h1><p>Generated from the current tracker.</p><ul>'
            + "".join(
                f"<li>{html.escape(r['company'])}: {html.escape(r['role'])}. {html.escape(r['status'])}</li>"
                for r in rows
            )
            + "</ul>",
        }

    def save_snapshot(self, candidate, path, snapshot):
        sid = digest([candidate, snapshot["hash"]])
        self.db.execute(
            "INSERT INTO tracker_snapshots(id,candidate,path,hash,records,totals) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                sid,
                candidate,
                str(path),
                snapshot["hash"],
                js(snapshot["records"]),
                js(snapshot["totals"]),
            ),
        )

    def check_hash(self, app, expected_hash):
        actual = file_hash(self.path(self.root(app["candidate"]), TRACKER_NAME))
        if actual != expected_hash:
            self.db.execute(
                "INSERT INTO conflicts(id,kind,subject,detail) VALUES(%s,'tracker_external_edit',%s,%s)",
                (uid(), app["id"], js({"expected": expected_hash, "actual": actual})),
            )
            raise ValueError(
                "Tracker changed externally; import and resolve before writing"
            )

    def finish(self, root, journal, entry):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", entry["application"]):
            raise ValueError("Invalid application in recovery journal")
        tracker = self.path(root, TRACKER_NAME)
        if file_hash(tracker) != entry["new_hash"]:
            raise ValueError(
                "Tracker changed after replacement; recovery will not overwrite external edits"
            )
        expected = {
            f"_Agent/Application_Notes/{entry['application']}.md",
            "_Agent/AGENT_HANDOFF.md",
            "_Agent/Application_Checklist.html",
        }
        if set(entry["sidecars"]) != expected:
            raise ValueError("Unexpected generated record in recovery journal")
        for record in entry["sidecars"].values():
            if digest(record["content"].encode()) != record["after_hash"]:
                raise ValueError("Recovery record content has changed")
        for rel, record in entry["sidecars"].items():
            target = self.path(root, rel)
            current = file_hash(target) if target.exists() else None
            if current == record["after_hash"]:
                continue
            if current != record["before_hash"]:
                raise ValueError(
                    "Generated notes changed externally; resolve the recovery journal before continuing"
                )
            atomic_write(target, record["content"])
        self.save_snapshot(entry["candidate"], tracker, read_ods(tracker))
        self.db.audit(
            "tracker.updated",
            entry["application"],
            {"journal": str(journal), "hash": entry["new_hash"]},
        )
        entry["state"] = "committed"
        atomic_write(journal, json.dumps(entry, indent=2))

    def update(self, aid, expected_hash, reviewed_hash):
        app = self.application(aid)
        self.allowed(app["candidate"])
        root = self.root(app["candidate"])
        with self.path(root, ".tracker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.check_hash(app, expected_hash)
            plan, app, snapshot = self.prepare(aid)
            if reviewed_hash != plan["review_hash"]:
                raise ValueError(
                    "The tracker preview changed; review it again before writing"
                )
            if plan["action"] == "already_recorded":
                self.save_snapshot(
                    app["candidate"], Path(plan["tracker_path"]), snapshot
                )
                return {
                    "action": "already_recorded",
                    "hash": snapshot["hash"],
                    "totals": snapshot["totals"],
                    "message": plan["reason"],
                }
            for old in self.path(root, "_Recovery").glob("*.json"):
                if json.loads(old.read_text()).get("state") not in (
                    "committed",
                    "aborted",
                ):
                    raise ValueError(
                        "Finish the pending tracker recovery journal before another update"
                    )
            content, after = self.archive(plan)
            entry_id = uid()
            backup = self.path(root, f"_Backups/{entry_id}.ods")
            path = Path(plan["tracker_path"])
            atomic_write(backup, path.read_bytes())
            sidecars = {}
            for rel, text in self.sidecar_content(plan, after).items():
                target = self.path(root, rel)
                current = file_hash(target) if target.exists() else None
                if current != plan["sidecars"][rel]:
                    raise ValueError("Generated notes changed after preview")
                side_backup = self.path(root, f"_Backups/{entry_id}/{rel}")
                if target.exists():
                    atomic_write(side_backup, target.read_bytes())
                sidecars[rel] = {
                    "before_hash": current,
                    "after_hash": digest(text.encode()),
                    "content": text,
                    "backup": str(side_backup) if current else None,
                }
            journal = self.path(root, f"_Recovery/{entry_id}.json")
            entry = {
                "state": "prepared",
                "candidate": app["candidate"],
                "application": aid,
                "tracker": str(path),
                "backup": str(backup),
                "old_hash": expected_hash,
                "new_hash": digest(content),
                "totals": after["totals"],
                "sidecars": sidecars,
                "review_hash": reviewed_hash,
            }
            atomic_write(journal, json.dumps(entry, indent=2))
            if file_hash(path) != expected_hash:
                entry["state"] = "aborted"
                atomic_write(journal, json.dumps(entry, indent=2))
                raise ValueError("Tracker changed during update")
            self.allowed(app["candidate"])
            if self.prepare(aid)[0]["review_hash"] != reviewed_hash:
                entry["state"] = "aborted"
                atomic_write(journal, json.dumps(entry, indent=2))
                raise ValueError(
                    "Application or evidence changed; review a fresh tracker preview"
                )
            atomic_write(path, content)
            entry["state"] = "tracker_replaced"
            atomic_write(journal, json.dumps(entry, indent=2))
            try:
                self.finish(root, journal, entry)
            except Exception:
                raise ValueError(
                    "Tracker saved, but generated records need recovery. Run tracker recovery before syncing again."
                ) from None
            return {
                "action": plan["action"],
                "totals": after["totals"],
                "hash": digest(content),
                "journal": str(journal),
            }

    def recover(self, candidate="fixture"):
        self.allowed(candidate)
        root = self.root(candidate)
        results = []
        with self.path(root, ".tracker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for journal in self.path(root, "_Recovery").glob("*.json"):
                entry = json.loads(journal.read_text())
                if entry["state"] in ("committed", "aborted"):
                    continue
                if (
                    entry.get("candidate") != candidate
                    or entry.get("tracker") != str(root / TRACKER_NAME)
                    or not entry.get("sidecars")
                ):
                    results.append(
                        {
                            "journal": str(journal),
                            "recovered": False,
                            "reason": "Legacy or mismatched journal requires manual review",
                        }
                    )
                    continue
                current = file_hash(self.path(root, TRACKER_NAME))
                if current == entry["old_hash"]:
                    entry["state"] = "aborted"
                    atomic_write(journal, json.dumps(entry, indent=2))
                    results.append(
                        {
                            "journal": str(journal),
                            "recovered": False,
                            "reason": "No replacement occurred; review a fresh preview",
                        }
                    )
                else:
                    try:
                        self.finish(root, journal, entry)
                        results.append({"journal": str(journal), "recovered": True})
                    except ValueError as exc:
                        results.append(
                            {
                                "journal": str(journal),
                                "recovered": False,
                                "reason": str(exc),
                            }
                        )
        return results
