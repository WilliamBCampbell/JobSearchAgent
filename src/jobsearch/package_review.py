"""Version-bound user feedback and document review; never submission approval."""

import json
from pathlib import Path
from pydantic import BaseModel, Field
from .config import file_hash
from .db import js, uid
from .documents.contracts import PackageDraft
from .workflow import Workflow


class PackageRevisionInput(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    notes: str = Field(min_length=1, max_length=2000)


class PackageReviewInput(BaseModel):
    version: str = Field(min_length=1, max_length=64)


EDITABLE = {
    "scored",
    "selected",
    "prepared",
    "review",
    "authorized",
    "manual_handoff",
    "missing_answers",
    "preparing",
}


class PackageReview:
    def __init__(self, db):
        self.db = db
        self.flow = Workflow(db)

    def blocker(self, app, conn):
        if app["imported"]:
            return "Imported packages are read only."
        if conn.execute(
            "SELECT 1 FROM attempts WHERE application_id=%s UNION SELECT 1 FROM receipts WHERE application_id=%s",
            (app["id"], app["id"]),
        ).fetchone():
            return "This application has a submission record. Reconcile its outcome before changing documents."
        if app["state"] not in EDITABLE:
            return "Document changes are available after preparation and before submission."
        if app["paused"]:
            return "Resume this application before requesting changes."
        if not app["payload"].get("package", {}).get("version"):
            return "Generate a versioned package before requesting changes."
        if conn.execute(
            "SELECT 1 FROM jobs WHERE input->>'application_id'=%s AND status='running'",
            (app["id"],),
        ).fetchone():
            return (
                "Wait for the current work to finish, or cancel it under Work progress."
            )
        return None

    def status(self, app):
        with self.db.tx() as c:
            blocker = self.blocker(app, c)
            rows = c.execute(
                "SELECT id,status,error,input->'user_revision' AS request,result->>'version' AS result_version,created_at FROM jobs WHERE input->>'application_id'=%s AND input ? 'user_revision' ORDER BY created_at DESC,id DESC",
                (app["id"],),
            ).fetchall()
            active = c.execute(
                "SELECT 1 FROM jobs WHERE input->>'application_id'=%s AND kind='package' AND status IN ('queued','running')",
                (app["id"],),
            ).fetchone()
        version = app["payload"].get("package", {}).get("version")
        return {
            "version": version,
            "reviewed": bool(
                version
                and app["payload"].get("document_review", {}).get("version") == version
            ),
            "blocker": blocker
            or (
                "Document preparation is queued. Review the new version when it finishes."
                if active
                else None
            ),
            "can_revise": not blocker and not active,
            "can_accept": not blocker and not active and app["state"] != "preparing",
            "history": [
                {
                    "job_id": r["id"],
                    "status": r["status"],
                    "error": r["error"],
                    "notes": r["request"]["notes"],
                    "base_version": r["request"]["base_version"],
                    "result_version": r["result_version"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        }

    def draft(self, app):
        try:
            return self._draft(app)
        except (OSError, KeyError, TypeError) as exc:
            raise ValueError(
                "The saved package is incomplete or unavailable; restore its files before requesting changes"
            ) from exc

    def _draft(self, app):
        package = app["payload"]["package"]
        folder = Path(package["review_path"]).parent.resolve()
        roots = [
            self.db.settings.fixture_workspace.resolve(),
            (self.db.settings.state / "prepared-workspace").resolve(),
        ]
        if not any(folder.is_relative_to(root) for root in roots):
            raise ValueError("The package is outside the configured document workspace")
        manifest = json.loads((folder / "Package_Manifest.json").read_text())
        if manifest["version"] != package["version"]:
            raise ValueError("The package version changed; refresh the review")
        if "Document_Content.json" not in manifest["files"]:
            raise ValueError("The prior draft has no recorded file hash")
        for name, expected in manifest["files"].items():
            path = (folder / name).resolve()
            if (
                not path.is_relative_to(folder)
                or not path.is_file()
                or file_hash(path) != expected
            ):
                raise ValueError(
                    "A package file changed; restore the saved version before revising"
                )
        return PackageDraft.model_validate_json(
            (folder / "Document_Content.json").read_text()
        ).model_dump()

    def revise(self, aid, data):
        notes = data.notes.strip()
        if not notes:
            raise ValueError("Describe the changes you want")
        with self.db.tx() as c:
            app = self.flow.get(aid, c)
            jobs = c.execute(
                "SELECT id,status,kind,input FROM jobs WHERE input->>'application_id'=%s AND status IN ('queued','running') FOR UPDATE",
                (aid,),
            ).fetchall()
            # Repeated clicks return the original queued request without replacing its notes.
            for job in jobs:
                old = job["input"].get("user_revision", {})
                if (
                    old.get("base_version") == data.version
                    and old.get("notes") == notes
                ):
                    return {"job_id": job["id"], "status": job["status"]}
            blocked = self.blocker(app, c)
            if blocked:
                raise ValueError(blocked)
            if any(j["kind"] == "package" for j in jobs):
                raise ValueError(
                    "A package is already queued; wait or cancel it before changing the request"
                )
            package = app["payload"]["package"]
            if package["version"] != data.version:
                raise ValueError(
                    "The package changed. Review the latest version before requesting changes"
                )
            previous = self.draft(app)
            jid = uid()
            selected = c.execute(
                "SELECT kind FROM artifacts WHERE application_id=%s AND id=ANY(%s)",
                (aid, app["payload"].get("attachments", [])),
            ).fetchall()
            request = {
                "base_version": data.version,
                "notes": notes,
                "previous_document": previous,
                "attachment_kinds": [r["kind"] for r in selected],
            }
            c.execute(
                "UPDATE jobs SET status='cancelled',cancel_requested=true,updated_at=now() WHERE id=ANY(%s)",
                ([j["id"] for j in jobs],),
            )
            c.execute(
                "UPDATE approvals SET invalidated=true WHERE application_id=%s", (aid,)
            )
            c.execute(
                "UPDATE applications SET payload=payload-'browser_hash'-'handoff_review'-'document_review',revision=revision+1,updated_at=now() WHERE id=%s",
                (aid,),
            )
            if app["state"] != "preparing":
                self.flow.transition(aid, "preparing", {"revision_job": jid}, c)
            c.execute(
                "INSERT INTO jobs(id,kind,input,max_attempts) VALUES(%s,'package',%s,2)",
                (
                    jid,
                    js(
                        {
                            "application_id": aid,
                            "include_cover": any(
                                f["kind"].startswith("cover_letter.")
                                for f in package["files"]
                            ),
                            "user_revision": request,
                        }
                    ),
                ),
            )
            self.db.audit(
                "package.revision_requested",
                aid,
                {"job_id": jid, "base_version": data.version},
                c,
            )
        return {"job_id": jid, "status": "queued"}

    def accept(self, aid, data):
        with self.db.tx() as c:
            app = self.flow.get(aid, c)
            blocked = self.blocker(app, c)
            if blocked:
                raise ValueError(blocked)
            if app["state"] == "preparing":
                raise ValueError(
                    "Finish the requested document preparation before recording your review"
                )
            if c.execute(
                "SELECT 1 FROM jobs WHERE input->>'application_id'=%s AND kind='package' AND status IN ('queued','running')",
                (aid,),
            ).fetchone():
                raise ValueError(
                    "Wait for document preparation before recording your review"
                )
            if app["payload"]["package"]["version"] != data.version:
                raise ValueError("The package changed. Review the latest version")
            self.draft(app)
            c.execute(
                "UPDATE applications SET payload=jsonb_set(payload,'{document_review}',%s),updated_at=now() WHERE id=%s",
                (js({"version": data.version}), aid),
            )
            self.db.audit("package.user_reviewed", aid, {"version": data.version}, c)
        return {"reviewed": True, "version": data.version}
