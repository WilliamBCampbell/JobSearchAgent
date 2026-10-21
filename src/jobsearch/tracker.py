from __future__ import annotations
from collections import Counter
import html
import json
import re
import zipfile
from lxml import etree as E
from .config import atomic_write, digest, file_hash
from .db import js

NS = {
    "t": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "x": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "o": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
}
T, X, O = ["{" + NS[k] + "}" for k in ("t", "x", "o")]
TRACKER_NAME = "Job Applications 2026 Real Hunt.ods"
ACTIVE = {"Submitted", "Applied, pending", "Applied, no response", "Interview", "Offer"}


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def requisition(role):
    matches = re.findall(r"\(([^()]*)\)", role)
    return next(
        (
            s.removeprefix("Requisition: ")
            for s in reversed(matches)
            if s.startswith("Requisition: ") or re.search(r"\d", s)
        ),
        "",
    )


def cells(row):
    return row.findall("t:table-cell", NS)


def cell_text(cell):
    return "\n".join(
        "".join(p.itertext()) for p in cell.findall("x:p", NS)
    ) or cell.get(O + "value", "")


def read_ods(path):
    with zipfile.ZipFile(path) as z:
        root = E.fromstring(
            z.read("content.xml"),
            parser=E.XMLParser(resolve_entities=False, no_network=True),
        )
    sheets = []
    for table in root.findall(".//t:table", NS):
        rows = []
        for row in table.findall("t:table-row", NS):
            values = []
            for c in cells(row):
                n = min(int(c.get(T + "number-columns-repeated", "1")), 256)
                values.extend([cell_text(c)] * n)
            while values and not values[-1]:
                values.pop()
            count = min(int(row.get(T + "number-rows-repeated", "1")), 10000)
            if any(values):
                rows.extend([values] * count)
        header = next(
            (
                i
                for i, r in enumerate(rows)
                if len(r) > 3 and r[0].lower() == "company" and r[3].lower() == "status"
            ),
            None,
        )
        sheets.append(
            {
                "name": table.get(T + "name", "Sheet"),
                "rows": rows,
                "header_index": header,
            }
        )
    records = []
    for sheet in sheets:
        if sheet["header_index"] is None:
            continue
        for i, values in enumerate(
            sheet["rows"][sheet["header_index"] + 1 :], sheet["header_index"] + 2
        ):
            v = values + [""] * 8
            if not v[0] or not v[1]:
                continue
            records.append(
                dict(
                    sheet=sheet["name"],
                    row=i,
                    company=v[0],
                    role=v[1],
                    requisition=requisition(v[1]),
                    date_applied=v[2],
                    status=v[3],
                    last_update=v[4],
                    resume=v[5],
                    cover_letter=v[6],
                    notes=v[7],
                    raw=values,
                )
            )
    return {
        "sheets": sheets,
        "records": records,
        "totals": totals_for(records),
        "hash": digest(path.getvalue())
        if hasattr(path, "getvalue")
        else file_hash(path),
    }


def totals_for(records):
    counts = Counter(r["status"] for r in records)
    active = sum(counts[x] for x in ACTIVE)
    return {
        "tracked": len(records),
        "submitted": active + counts["Rejected"],
        "active": active,
        "rejected": counts["Rejected"],
        "prepared": counts["Prepared, not submitted"],
        "offers": counts["Offer"],
        "unresolved": counts["Unresolved submission"],
        "closed": sum(n for s, n in counts.items() if "closed" in s.lower()),
        "deprioritized": sum(
            n for s, n in counts.items() if "deprioritized" in s.lower()
        ),
    }


def state_for(status):
    if status in ACTIVE or status == "Rejected":
        return "submitted" if status != "Rejected" else "rejected"
    if "unresolved" in status.lower():
        return "unresolved"
    if "closed" in status.lower():
        return "closed"
    if "deprioritized" in status.lower():
        return "skipped"
    return "prepared"


class Tracker:
    def __init__(self, db):
        self.db = db

    def import_snapshot(self, candidate="real"):
        root = (
            self.db.settings.workspace
            if candidate == "real"
            else self.db.settings.fixture_workspace
        )
        path = root / TRACKER_NAME
        snapshot = read_ods(path)
        sid = digest([candidate, snapshot["hash"]])
        conflict_ids = []
        with self.db.tx() as c:
            c.execute("SELECT pg_advisory_xact_lock(881904)")
            c.execute(
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
            from .tracker_write import TrackerWriter, employer_key

            writer = TrackerWriter(self.db)
            current_apps = c.execute(
                "SELECT * FROM applications WHERE candidate=%s", (candidate,)
            ).fetchall()
            for r in snapshot["records"]:
                req = (
                    r["requisition"]
                    or "legacy-"
                    + digest([normalize(r["company"]), normalize(r["role"])])[:12]
                )
                aid = digest([candidate, normalize(r["company"]), req])[:32]
                matches = [
                    a
                    for a in current_apps
                    if a["id"] == aid
                    or (
                        employer_key(a["employer"]) == employer_key(r["company"])
                        and req in writer.identifiers(a)[0]
                    )
                ]
                if len(matches) > 1:
                    raise ValueError(
                        "Multiple applications match a tracker row; resolve the duplicate identities before importing"
                    )
                old = matches[0] if matches else None
                if old:
                    aid = old["id"]
                state = state_for(r["status"])
                if old and not old["imported"]:
                    # Importing never grants a workflow a receipt or reverses its attempt history.
                    if old["state"] != state and not (
                        state == "prepared" and old["state"] == "deferred"
                    ):
                        cid = digest([aid, sid])
                        c.execute(
                            "INSERT INTO conflicts(id,kind,subject,detail) VALUES(%s,'tracker_workflow_status',%s,%s) ON CONFLICT DO NOTHING",
                            (
                                cid,
                                aid,
                                js(
                                    {
                                        "database": old["state"],
                                        "ods": state,
                                        "record": r,
                                        "snapshot": sid,
                                    }
                                ),
                            ),
                        )
                        conflict_ids.append(cid)
                    continue
                payload = {
                    "tracker": r,
                    "tracker_hash": snapshot["hash"],
                    "tracker_snapshot": sid,
                    "answers": {},
                    "attachments": [],
                }
                if (
                    old
                    and old["payload"].get("tracker", {}).get("status") != r["status"]
                ):
                    cid = digest([aid, sid])
                    c.execute(
                        "INSERT INTO conflicts(id,kind,subject,detail) VALUES(%s,'tracker_status',%s,%s) ON CONFLICT DO NOTHING",
                        (
                            cid,
                            aid,
                            js(
                                {
                                    "database": old["state"],
                                    "ods": state,
                                    "snapshot": sid,
                                    "record": r,
                                }
                            ),
                        ),
                    )
                    conflict_ids.append(cid)
                    continue
                c.execute(
                    "INSERT INTO applications(id,candidate,employer,role,requisition,state,payload,imported) VALUES(%s,%s,%s,%s,%s,%s,%s,true) ON CONFLICT(id) DO NOTHING",
                    (aid, candidate, r["company"], r["role"], req, state, js(payload)),
                )
            self.db.audit(
                "tracker.imported",
                sid,
                {
                    "candidate": candidate,
                    "records": len(snapshot["records"]),
                    "conflicts": len(conflict_ids),
                },
                c,
            )
        return {
            "snapshot": sid,
            "totals": snapshot["totals"],
            "conflicts": conflict_ids,
            "hash": snapshot["hash"],
        }

    def resolve_import_conflict(self, conflict_id, choice):
        if choice != "ods":
            raise ValueError(
                "Imported records accept the authoritative ODS status; application sync uses a separate reviewed preview"
            )
        external = self.db.one(
            "SELECT * FROM conflicts WHERE id=%s AND kind IN ('tracker_external_edit','tracker_workflow_status') AND NOT resolved",
            (conflict_id,),
        )
        if external:
            app = self.db.one(
                "SELECT * FROM applications WHERE id=%s", (external["subject"],)
            )
            from .tracker_write import TrackerWriter

            writer = TrackerWriter(self.db)
            path = writer.path(writer.root(app["candidate"]), TRACKER_NAME)
            snapshot = read_ods(path)
            writer.save_snapshot(app["candidate"], path, snapshot)
            self.db.execute(
                "UPDATE conflicts SET resolved=true,resolution=%s WHERE id=%s",
                (js({"choice": "ods", "hash": snapshot["hash"]}), conflict_id),
            )
            self.db.audit(
                "tracker.external_edit_acknowledged",
                app["id"],
                {"hash": snapshot["hash"]},
            )
            return {"resolved": True, "requires_fresh_preview": True}
        with self.db.tx() as c:
            conflict = c.execute(
                "SELECT * FROM conflicts WHERE id=%s AND kind='tracker_status' AND NOT resolved FOR UPDATE",
                (conflict_id,),
            ).fetchone()
            if not conflict:
                raise ValueError("Conflict not found")
            detail = conflict["detail"]
            app = c.execute(
                "SELECT * FROM applications WHERE id=%s FOR UPDATE",
                (conflict["subject"],),
            ).fetchone()
            payload = app["payload"] | {
                "tracker": detail["record"],
                "tracker_snapshot": detail["snapshot"],
            }
            c.execute(
                "UPDATE applications SET state=%s,payload=%s,revision=revision+1 WHERE id=%s",
                (detail["ods"], js(payload), app["id"]),
            )
            c.execute(
                "UPDATE conflicts SET resolved=true,resolution=%s WHERE id=%s",
                (js({"choice": "ods"}), conflict_id),
            )
            self.db.audit("tracker.conflict_resolved", app["id"], {"choice": "ods"}, c)
        return {"resolved": True}

    def preview(self, application_id):
        from .tracker_write import TrackerWriter

        return TrackerWriter(self.db).prepare(application_id)[0]

    def update(self, application_id, expected_hash, reviewed_hash):
        from .tracker_write import TrackerWriter

        return TrackerWriter(self.db).update(
            application_id, expected_hash, reviewed_hash
        )

    def update_fixture(self, application_id, expected_hash):
        from .tracker_write import TrackerWriter

        app = self.db.one("SELECT * FROM applications WHERE id=%s", (application_id,))
        if not app or app["candidate"] != "fixture":
            raise ValueError("This helper only writes fixture trackers")
        writer = TrackerWriter(self.db)
        writer.check_hash(app, expected_hash)
        plan = self.preview(application_id)
        return self.update(application_id, expected_hash, plan["review_hash"])

    def recover(self, candidate="fixture"):
        from .tracker_write import TrackerWriter

        return TrackerWriter(self.db).recover(candidate)

    def manual_report(self, candidate="fixture", on_demand=False):
        entries = self.db.all(
            "SELECT f.*,a.employer,a.role,a.requisition,a.state,a.official,a.payload FROM followups f LEFT JOIN applications a ON a.id=f.application_id WHERE f.candidate=%s AND NOT f.resolved AND f.category='manual_application' ORDER BY f.id",
            (candidate,),
        )
        key = "manual_report_latched_" + candidate
        latched = self.db.setting(key, False)
        if len(entries) < 10:
            self.db.set_setting(key, False)
        if not on_demand and (len(entries) < 10 or latched):
            return {"count": len(entries), "generated": False}
        fingerprint = digest([(e["id"], e["detail"]) for e in entries])
        existing = self.db.one(
            "SELECT * FROM reports WHERE fingerprint=%s", (fingerprint,)
        )
        if existing:
            return {"count": len(entries), "generated": False, "path": existing["path"]}
        root = self.db.settings.state / "reports"
        path = root / f"manual-{fingerprint[:12]}.html"
        sections = []
        for e in entries:
            content = {
                k: e.get(k)
                for k in (
                    "employer",
                    "role",
                    "requisition",
                    "state",
                    "official",
                    "detail",
                )
            }
            content["receipt_evidence"] = (
                self.db.all(
                    "SELECT kind,evidence FROM receipts WHERE application_id=%s",
                    (e["application_id"],),
                )
                if e["application_id"]
                else []
            )
            content["next_step"] = (
                "Check receipt evidence before retrying. Open the official route in your normal browser and use the saved answers and attachments."
            )
            sections.append(
                "<section><pre>"
                + html.escape(json.dumps(content, indent=2, default=str))
                + "</pre></section>"
            )
        atomic_write(
            path,
            '<!doctype html><meta charset="utf-8"><h1>Manual application follow-up</h1>'
            + "".join(sections),
        )
        self.db.execute(
            "INSERT INTO reports(fingerprint,path) VALUES(%s,%s)",
            (fingerprint, str(path)),
        )
        if len(entries) >= 10:
            self.db.set_setting(key, True)
        return {"count": len(entries), "generated": True, "path": str(path)}
