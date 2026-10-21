from __future__ import annotations
from collections import Counter
import json
from pathlib import Path
import re
from .config import atomic_write, digest, file_hash
from .corpus import Corpus, SUPPORTED, EXCLUDED, SECRET_KEYS
from .db import js
from .memory import FactInput, Memory, SCOPED
from .tracker import Tracker, TRACKER_NAME, normalize

GLOBAL_KEYS = {
    "first_name",
    "last_name",
    "email",
    "phone",
    "city",
    "state",
    "country",
    "linkedin",
    "website",
    "github",
    "work_authorization",
    "dotnet_core_csharp_years",
    "postgresql_years",
    "vue_years",
    "vue_employer",
    "vue_job_title",
    "pronouns",
}
META_KEYS = {"source", "source_additional", "confirmed_at", "confirmed_date"}


class Intake:
    def __init__(self, db):
        self.db = db
        self.corpus = Corpus(db)
        self.memory = Memory(db)

    def inventory(self, candidate="real"):
        root = (
            self.db.settings.workspace
            if candidate == "real"
            else self.db.settings.fixture_workspace
        )
        entries = []
        for p in root.rglob("*"):
            if (
                not p.is_file()
                or p.is_symlink()
                or any(
                    x in EXCLUDED or x.startswith(".")
                    for x in p.relative_to(root).parts
                )
            ):
                continue
            if p.suffix.lower() not in SUPPORTED or SECRET_KEYS.search(p.name):
                continue
            entries.append(
                {
                    "path": str(p.relative_to(root)),
                    "bytes": p.stat().st_size,
                    "hash": file_hash(p),
                }
            )
        result = {
            "candidate": candidate,
            "root": str(root),
            "files": entries,
            "formats": dict(Counter(Path(e["path"]).suffix.lower() for e in entries)),
            "fingerprint": digest(entries),
        }
        atomic_write(
            self.db.settings.state / f"{candidate}-workspace-inventory.json",
            json.dumps(result, indent=2),
        )
        return result

    def run(self, candidate="real"):
        inventory = self.inventory(candidate)
        tracker = Tracker(self.db).import_snapshot(candidate)
        selected = [
            e
            for e in inventory["files"]
            if Path(e["path"]).name
            in (
                "AGENT_HANDOFF.md",
                "job_search_workflow.md",
                TRACKER_NAME,
                "Unresolved_Submissions.json",
            )
            or "Resume-Sources" in e["path"]
            or Path(e["path"]).name
            in (
                "Application_Answers.json",
                "Confirmed_Application_Answers.json",
                "Submission_Receipt.json",
            )
        ]
        results = []
        fact_count = 0
        for e in selected:
            result = self.corpus.ingest(e["path"], candidate)
            results.append(result)
            path = self.db.settings.source_path(e["path"], candidate)
            if path.name == "job_search_workflow.md":
                fact_count += self.import_current_employment(candidate, path, result)
                fact_count += self.import_confirmed_career(candidate, path, result)
            if path.name == "AGENT_HANDOFF.md":
                fact_count += self.import_confirmed_career(candidate, path, result)
            if path.name == "Submission_Receipt.json":
                self.import_receipt(candidate, path, e["path"], result)
            if path.name not in (
                "Application_Answers.json",
                "Confirmed_Application_Answers.json",
            ):
                continue
            data = json.loads(path.read_text())
            fact_count += self.import_confirmed_education(candidate, path, result, data)
            employer = data.get("company", data.get("employer", ""))
            req = str(data.get("requisition", ""))
            apps = self.db.all(
                "SELECT * FROM applications WHERE candidate=%s", (candidate,)
            )
            app = next(
                (
                    a
                    for a in apps
                    if (req and req == a["requisition"])
                    or (
                        normalize(a["employer"]) == normalize(employer)
                        and len(
                            [
                                x
                                for x in apps
                                if normalize(x["employer"]) == normalize(employer)
                            ]
                        )
                        == 1
                    )
                ),
                None,
            )
            if not app:
                continue
            confirmed = data.get("confirmed", {})
            if not isinstance(confirmed, dict):
                continue
            for key, value in confirmed.items():
                if key in META_KEYS or SECRET_KEYS.search(key) or value is None:
                    continue
                global_fact = key in GLOBAL_KEYS
                raw_value = value
                if global_fact and isinstance(value, str):
                    if key == "phone":
                        digits = re.sub(r"\D", "", value)
                        if len(digits) == 10:
                            digits = "1" + digits
                        value = "+" + digits
                    elif key == "country" and value.lower() in (
                        "us",
                        "usa",
                        "united states",
                        "united states of america",
                    ):
                        value = "United States"
                    elif key == "city":
                        value = value.split(",")[0].strip()
                    elif key in ("website", "linkedin", "github"):
                        value = value.rstrip("/")
                prior = self.db.one(
                    "SELECT * FROM facts WHERE candidate=%s AND key=%s AND scope=%s AND active AND provenance->>'source_path'=%s ORDER BY verified_at DESC",
                    (candidate, key, "global" if global_fact else app["id"], e["path"]),
                )
                kind = (
                    "skill_duration"
                    if key.endswith("_years")
                    else "candidate"
                    if global_fact
                    else "answer"
                )
                status = (
                    "confirmed" if global_fact or SCOPED.search(key) else "unconfirmed"
                )
                # Only explicitly labeled confirmed fields are imported. Narrative drafts stay unconfirmed.
                try:
                    self.memory.put(
                        FactInput(
                            candidate=candidate,
                            kind=kind,
                            key=key,
                            value=value,
                            status=status,
                            scope="global" if global_fact else app["id"],
                            provenance={
                                "type": "explicit_record",
                                "source_path": e["path"],
                                "json_pointer": "/confirmed/" + key,
                                "raw_value": raw_value,
                                "recorded_confirmation": confirmed.get(
                                    "source_additional",
                                    confirmed.get(
                                        "source",
                                        data.get("source", "Confirmed answers record"),
                                    ),
                                ),
                            },
                            evidence_versions=[result["version"]],
                            supersedes=prior["id"]
                            if prior and prior["value"] != value
                            else None,
                        ),
                        actor="importer",
                    )
                    fact_count += 1
                except ValueError:
                    continue
            status = str(data.get("status", ""))
            if "deferred" in status.lower():
                # ODS status stays prepared; deferral is a separate decision with its own provenance.
                self.memory.put(
                    FactInput(
                        candidate=candidate,
                        kind="decision",
                        key="deferral",
                        value={"status": status, "pending": data.get("pending", [])},
                        status="confirmed",
                        scope=app["id"],
                        provenance={
                            "type": "explicit_record",
                            "source_path": e["path"],
                            "json_pointer": "/status",
                        },
                        evidence_versions=[result["version"]],
                    ),
                    actor="importer",
                )
            if data.get("submitted_documents"):
                self.db.audit(
                    "historical.attachments_imported",
                    app["id"],
                    {
                        "source_document": result["id"],
                        "attached_count": len(data["submitted_documents"]),
                    },
                )
        # Preserve the existing manual queue without treating ordinary unanswered questions as submissions.
        root = (
            self.db.settings.workspace
            if candidate == "real"
            else self.db.settings.fixture_workspace
        )
        qpath = root / "Unresolved_Submissions.json"
        if qpath.exists():
            queue = json.loads(qpath.read_text())
            self.db.set_setting("imported_manual_queue_" + candidate, queue)
            qdoc = self.db.one(
                "SELECT * FROM documents WHERE candidate=%s AND path=%s",
                (candidate, "Unresolved_Submissions.json"),
            )
            if qdoc:
                self.db.execute(
                    "UPDATE documents SET metadata=%s WHERE id=%s",
                    (js({"confirmation_sections": ["manual_queue"]}), qdoc["id"]),
                )
            for entry in queue.get("entries", []):
                apps = self.db.all(
                    "SELECT * FROM applications WHERE candidate=%s", (candidate,)
                )
                app = next(
                    (
                        a
                        for a in apps
                        if a["requisition"] == str(entry.get("requisition"))
                    ),
                    None,
                )
                if app and qdoc and "defer" in str(entry.get("state", "")).lower():
                    self.memory.put(
                        FactInput(
                            candidate=candidate,
                            kind="decision",
                            key="deferral",
                            value={
                                "status": entry.get("state"),
                                "reason": entry.get("reason"),
                                "next_action": entry.get("next_action"),
                            },
                            status="confirmed",
                            scope=app["id"],
                            provenance={
                                "type": "explicit_record",
                                "source_path": "Unresolved_Submissions.json",
                                "confirmation_section": "manual_queue",
                            },
                            evidence_versions=[qdoc["current_version"]],
                        ),
                        actor="importer",
                    )
                fid = digest(
                    [
                        candidate,
                        "imported-manual",
                        entry.get("employer"),
                        entry.get("requisition"),
                    ]
                )
                self.db.execute(
                    "INSERT INTO followups(id,application_id,candidate,category,detail) VALUES(%s,%s,%s,'manual_application',%s) ON CONFLICT(id) DO UPDATE SET detail=EXCLUDED.detail",
                    (fid, app["id"] if app else None, candidate, js(entry)),
                )
        report = {
            "inventory": {
                "files": len(inventory["files"]),
                "formats": inventory["formats"],
                "fingerprint": inventory["fingerprint"],
            },
            "tracker": tracker,
            "documents": len(results),
            "facts_processed": fact_count,
            "extraction_warnings": sum(bool(r.get("warnings")) for r in results),
            "source_writes": 0,
            "note": "Original resumes remain historical. Conflicting confirmation records require explicit correction; filename dates do not resolve them.",
        }
        atomic_write(
            self.db.settings.state / f"{candidate}-intake-report.json",
            json.dumps(report, indent=2),
        )
        return report

    def import_confirmed_education(self, candidate, path, result, data):
        if path.name != "Confirmed_Application_Answers.json" or not data.get(
            "confirmed_date"
        ):
            return 0
        education = data.get("education", {})
        if (
            not isinstance(education, dict)
            or not education.get("training")
            or not education.get("training_completed_year")
        ):
            return 0
        key = "education_" + normalize(education["training"]).replace(" ", "_")
        value = " | ".join(
            str(v)
            for v in (
                education["training"],
                education.get("training_location"),
                "Completed " + str(education["training_completed_year"]),
            )
            if v
        )
        root = (
            self.db.settings.workspace
            if candidate == "real"
            else self.db.settings.fixture_workspace
        )
        relative = str(path.relative_to(root))
        self.memory.put(
            FactInput(
                candidate=candidate,
                key=key,
                kind="candidate",
                value=value,
                status="confirmed",
                provenance={
                    "type": "explicit_record",
                    "source_path": relative,
                    "json_pointer": "/education",
                    "recorded_confirmation": data["confirmed_date"],
                },
                evidence_versions=[result["version"]],
            ),
            actor="importer",
        )
        return 1

    def import_current_employment(self, candidate, path, result):
        section = "Employment history used in applications"
        text = path.read_text()
        match = re.search(
            r"### " + re.escape(section) + r"\n(.*?)(?=\n### |\n## |\Z)", text, re.S
        )
        if not match:
            return 0
        metadata = self.db.one(
            "SELECT metadata FROM documents WHERE id=%s", (result["id"],)
        )["metadata"]
        metadata["confirmation_sections"] = list(
            set(metadata.get("confirmation_sections", []) + [section])
        )
        self.db.execute(
            "UPDATE documents SET metadata=%s WHERE id=%s",
            (js(metadata), result["id"]),
        )
        count = 0
        for line in match.group(1).splitlines():
            parsed = re.match(
                r"- \*\*(.+), ([^,]+), (\d{2}/\d{4}) to (\d{2}/\d{4})\.\*\*(.*)", line
            )
            if not parsed:
                continue
            employer, title, start, end, note = parsed.groups()
            key = "employment_" + normalize(employer).replace(" ", "_")
            value = {
                "employer": employer,
                "title": title,
                "start": start,
                "end": end,
                "notes": note.strip(),
            }
            prior = self.db.one(
                "SELECT * FROM facts WHERE candidate=%s AND kind='employment' AND active AND provenance->>'quote'=%s ORDER BY verified_at DESC LIMIT 1",
                (candidate, line),
            )
            if prior:
                key = prior["key"]
            self.memory.put(
                FactInput(
                    candidate=candidate,
                    kind="employment",
                    key=key,
                    supersedes=prior["id"]
                    if prior and prior["value"] != value
                    else None,
                    value=value,
                    status="confirmed",
                    provenance={
                        "type": "explicit_record",
                        "source_path": str(
                            path.relative_to(
                                self.db.settings.workspace
                                if candidate == "real"
                                else self.db.settings.fixture_workspace
                            )
                        ),
                        "confirmation_section": section,
                        "quote": line,
                    },
                    evidence_versions=[result["version"]],
                ),
                actor="importer",
            )
            count += 1
        return count

    def import_receipt(self, candidate, path, relative, result):
        data = json.loads(path.read_text())
        apps = self.db.all(
            "SELECT * FROM applications WHERE candidate=%s", (candidate,)
        )
        app = next(
            (
                a
                for a in apps
                if a["requisition"] in relative
                and not a["requisition"].startswith("legacy-")
            ),
            None,
        )
        if not app:
            return
        rid = digest([app["id"], result["version"], "imported_receipt"])
        evidence = {
            "source_document": result["id"],
            "source_version": result["version"],
            "source_path": relative,
            "text": data.get(
                "text",
                data.get("confirmation", "Imported receipt; inspect source document"),
            ),
            "url": data.get("url"),
            "historical": True,
            "requisition": app["requisition"],
        }
        self.db.execute(
            "INSERT INTO receipts(id,application_id,kind,evidence) VALUES(%s,%s,'imported_employer_confirmation',%s) ON CONFLICT DO NOTHING",
            (rid, app["id"], js(evidence)),
        )
        self.memory.put(
            FactInput(
                candidate=candidate,
                kind="receipt",
                key="receipt_evidence",
                value=evidence,
                status="confirmed",
                scope=app["id"],
                provenance={"type": "explicit_record", "source_path": relative},
                evidence_versions=[result["version"]],
            ),
            actor="importer",
        )

    def import_confirmed_career(self, candidate, path, result):
        """Only designated confirmation sections in the canonical workflow qualify.

        Salary, schedules, permissions and application-specific commitments are
        excluded. General summaries elsewhere remain source claims.
        """
        if path.name == "job_search_workflow.md":
            sections = [
                "Confirmed technical and customer experience",
                "AI delivery and BAM answers confirmed October 8, 2026",
            ]
        elif path.name == "AGENT_HANDOFF.md":
            sections = ["Entity Framework, SQL, and AWS confirmed October 19, 2026"]
        else:
            return 0
        doc = self.db.one("SELECT metadata FROM documents WHERE id=%s", (result["id"],))
        metadata = doc["metadata"]
        metadata["confirmation_sections"] = list(
            set(metadata.get("confirmation_sections", []) + sections)
        )
        self.db.execute(
            "UPDATE documents SET metadata=%s WHERE id=%s", (js(metadata), result["id"])
        )
        count = 0
        for section in sections:
            match = re.search(
                r"### " + re.escape(section) + r"\n(.*?)(?=\n### |\n## |\Z)",
                path.read_text(),
                re.S,
            )
            if not match:
                continue
            paragraphs = match.group(1).strip().split("\n\n")
            if path.name == "AGENT_HANDOFF.md":
                paragraphs = paragraphs[:1]
            for paragraph in paragraphs:
                # The final BAM paragraph contains role-specific commitments.
                if paragraph.startswith("For BAM,"):
                    continue
                pieces = (
                    paragraph.splitlines()
                    if paragraph.startswith("- ")
                    else [paragraph]
                )
                for piece in pieces:
                    value = piece.removeprefix("- ").strip()
                    if not value:
                        continue
                    self.memory.put(
                        FactInput(
                            candidate=candidate,
                            kind="skill",
                            key="confirmed_career_" + digest(value)[:20],
                            value=value,
                            status="confirmed",
                            provenance={
                                "type": "explicit_record",
                                "source_path": path.name,
                                "confirmation_section": section,
                                "quote": piece,
                            },
                            evidence_versions=[result["version"]],
                        ),
                        actor="importer",
                    )
                    count += 1
        return count
