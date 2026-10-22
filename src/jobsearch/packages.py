from __future__ import annotations
import html
import json
import re
import tempfile
from pathlib import Path

from .config import atomic_write, digest, file_hash
from .db import js, uid
from .memory import Memory
from .corpus import Corpus
from .llm.contracts import InferenceRequest
from .llm.service import InferenceService
from .llm.runner import InferenceFailure, InferenceStopped
from .documents.content import claims, validate_document
from .documents.rendering import (
    VERSION,
    TEMPLATE_HASH,
    check_dependencies,
    document_html,
    validate_cover_letter_html,
    render,
)


def slug(value):
    return re.sub(r"[^a-zA-Z0-9.-]+", "-", value).strip("-")[:100] or "Application"


def prose_check(text):
    patterns = [
        r"—",
        r"\b(?:delve|leverage|utilize|foster|bolster|underscore|unveil|streamline)\b",
        r"\b(?:robust|comprehensive|pivotal|seamless|significantly|extremely|truly)\b",
        r"Furthermore|Moreover|That being said|In today.s world",
    ]
    return [p for p in patterns if re.search(p, text, re.I)]


class PackageIntegrityError(ValueError):
    pass


class Packages:
    def __init__(self, db):
        self.db = db

    def profile(self, app):
        facts = self.db.all(
            "SELECT * FROM facts WHERE candidate=%s AND active AND status='confirmed' AND kind='candidate' AND scope=ANY(%s) ORDER BY id",
            (app["candidate"], ["global", app["id"]]),
        )
        used = []

        def value(key):
            rows = [
                f
                for f in facts
                if f["key"] == key
                and Memory(self.db)._valid(
                    {"fact_ids": [f["id"]], "version_ids": f["evidence_versions"]}
                )
            ]
            scoped = [f for f in rows if f["scope"] == app["id"]]
            rows = scoped or rows
            values = {str(f["value"]) for f in rows}
            if len(values) > 1:
                raise ValueError(
                    f"Conflicting {key} records need correction before document preparation"
                )
            if rows:
                used.append(rows[0]["id"])
                return str(rows[0]["value"])
            return ""

        name = value("professional_name") or " ".join(
            filter(None, [value("first_name"), value("last_name")])
        )
        if not name:
            raise ValueError("Confirm the name to display on application documents")
        location = ", ".join(filter(None, [value("city"), value("state")])) or value(
            "location"
        )
        phone = value("phone")
        if re.fullmatch(r"\+1\d{10}", phone):
            phone = f"({phone[2:5]}) {phone[5:8]}-{phone[8:]}"
        contacts = " | ".join(
            filter(
                None,
                [
                    location,
                    phone,
                    value("email"),
                    value("website"),
                    value("linkedin"),
                    value("github"),
                ],
            )
        )
        return {"name": name, "contacts": contacts, "fact_ids": used}

    def prepare(
        self,
        app,
        include_cover=True,
        job_id=None,
        progress=lambda value: None,
        cancelled=lambda: False,
        user_revision=None,
    ):
        if app["imported"] or app["state"] in (
            "submitted",
            "submission_attempt",
            "receipt_verification",
            "unresolved",
        ):
            raise ValueError("Imported and attempted applications are read-only")
        check_dependencies()
        service = InferenceService(self.db)
        profile = self.profile(app)
        feedback = ""
        revision = user_revision or {}
        revision_fields = {
            "revision_notes": revision.get("notes", ""),
            "previous_package_document": revision.get("previous_document"),
        }
        run_ids = []
        for attempt in range(2):
            if cancelled():
                raise InferenceStopped("Document preparation cancelled")
            request = InferenceRequest(
                task="package_draft",
                candidate=app["candidate"],
                application_id=app["id"],
                question=feedback,
                **revision_fields,
            )
            packet = service.context(request)
            try:
                progress(
                    {
                        "stage": "Writing resume and cover letter",
                        "revision": attempt + 1,
                    }
                )
                drafted = service.run(request, job_id, progress, cancelled)
                run_ids.append(drafted["run_id"])
                document = validate_document(drafted["output"], packet)
                progress({"stage": "Checking claims and writing"})
                reviewed = service.run(
                    InferenceRequest(
                        task="package_review",
                        candidate=app["candidate"],
                        application_id=app["id"],
                        package_document=document,
                        **revision_fields,
                    ),
                    job_id,
                    progress,
                    cancelled,
                )
                run_ids.append(reviewed["run_id"])
                if not reviewed["output"]["passed"]:
                    raise InferenceFailure(
                        "Revise the package: " + "; ".join(reviewed["output"]["issues"])
                    )
                if (
                    digest(service.context(request)) != digest(packet)
                    or self.profile(app) != profile
                ):
                    raise ValueError(
                        "Career evidence changed during preparation; prepare again"
                    )
                result = self.publish(
                    app,
                    document,
                    packet,
                    profile,
                    include_cover,
                    run_ids,
                    reviewed["output"],
                    progress,
                    cancelled,
                )
                if (
                    digest(service.context(request)) != digest(packet)
                    or self.profile(app) != profile
                ):
                    raise PackageIntegrityError(
                        "Career evidence changed while rendering; prepare again"
                    )
                return result
            except (InferenceStopped, PackageIntegrityError):
                raise
            except ValueError as exc:
                # One bounded writing/layout revision. Successful inference is cached across restarts.
                if attempt or cancelled():
                    raise
                feedback = (
                    "Revise the document to address this validation feedback: "
                    + str(exc)[:3500]
                )
        raise ValueError("Document preparation did not complete")

    def publish(
        self,
        app,
        document,
        packet,
        profile,
        include_cover,
        run_ids,
        review_result,
        progress,
        cancelled,
    ):
        root = (
            self.db.settings.fixture_workspace
            if app["candidate"] == "fixture"
            else self.db.settings.state / "prepared-workspace"
        )
        version = digest(
            [
                VERSION,
                TEMPLATE_HASH,
                app["id"],
                app["employer"],
                app["role"],
                app["requisition"],
                document,
                packet,
                profile,
                include_cover,
                run_ids,
                app["payload"].get("answers", {}),
            ]
        )[:16]
        parent = (
            root
            / f"{slug(app['employer'])}/{slug(app['requisition'])}({slug(app['role'])})/versions"
        )
        folder = parent / version
        parent.mkdir(parents=True, exist_ok=True)
        records = dict(claims(document))
        all_ids = sorted(
            set(
                profile["fact_ids"]
                + [i for c in records.values() for i in c["fact_ids"]]
                + [r["employment_fact_id"] for r in document["experience"]]
            )
        )
        manifest_path = folder / "Package_Manifest.json"
        if not folder.exists():
            with tempfile.TemporaryDirectory(
                prefix=".preparing-", dir=parent
            ) as temporary:
                stage = Path(temporary)
                documents = []
                checks = {}
                for kind in ["resume", "cover_letter"] if include_cover else ["resume"]:
                    stem = f"{slug(profile['name']).replace('-', '_')}_{slug(app['employer'])}_{slug(app['requisition'])}_{'Resume' if kind == 'resume' else 'Cover_Letter'}"
                    source = stage / (stem + ".html")
                    rendered = document_html(
                        kind, document, packet["facts"], profile, app
                    )
                    if kind == "cover_letter":
                        validate_cover_letter_html(rendered, app)
                    atomic_write(source, rendered)
                    progress({"stage": "Rendering " + kind.replace("_", " ")})
                    checks[kind] = render(source, cancelled)
                    claim_ids = set(profile["fact_ids"])
                    for location, claim in records.items():
                        if location.startswith("cover_letter.") == (
                            kind == "cover_letter"
                        ):
                            claim_ids.update(claim["fact_ids"])
                    if kind == "resume":
                        claim_ids.update(
                            r["employment_fact_id"] for r in document["experience"]
                        )
                    for suffix in [".html", ".docx", ".pdf", ".preview.png"]:
                        path = stage / (stem + suffix)
                        documents.append(
                            {
                                "filename": path.name,
                                "kind": kind + suffix,
                                "hash": file_hash(path),
                                "claims": sorted(claim_ids),
                            }
                        )
                atomic_write(
                    stage / "Document_Content.json", json.dumps(document, indent=2)
                )
                atomic_write(
                    stage / "Application_Answers.json",
                    json.dumps(
                        {
                            "employer": app["employer"],
                            "role": app["role"],
                            "requisition": app["requisition"],
                            "answers": app["payload"].get("answers", {}),
                            "claim_fact_ids": all_ids,
                            "status": "Prepared; not submitted",
                        },
                        indent=2,
                    ),
                )
                atomic_write(
                    stage / "Document_Review.json",
                    json.dumps(
                        {
                            "renderer": VERSION,
                            "inference_runs": run_ids,
                            "content_review": review_result,
                            "claims": records,
                            "checks": checks,
                        },
                        indent=2,
                    ),
                )
                links = "".join(
                    f'<li><a href="{html.escape(d["filename"], quote=True)}">{html.escape(d["kind"])}</a></li>'
                    for d in documents
                )
                images = "".join(
                    f'<h2>{html.escape(k.replace("_", " ").title())}</h2><img style="max-width:100%" src="{html.escape(v["preview"], quote=True)}">'
                    for k, v in checks.items()
                )
                atomic_write(
                    stage / "Application_Review.html",
                    '<!doctype html><meta charset="utf-8"><title>Document review</title><h1>'
                    + html.escape(app["employer"] + " / " + app["role"])
                    + "</h1><p>Prepared for review. Not submitted. Check the rendered pages before selecting attachments.</p><ul>"
                    + links
                    + "</ul>"
                    + images,
                )
                manifest = {
                    "version": version,
                    "renderer": VERSION,
                    "documents": documents,
                    "checks": checks,
                    "files": {
                        p.name: file_hash(p) for p in stage.iterdir() if p.is_file()
                    },
                }
                atomic_write(
                    stage / "Package_Manifest.json", json.dumps(manifest, indent=2)
                )
                if cancelled():
                    raise InferenceStopped("Document preparation cancelled")
                stage.rename(folder)
        if not manifest_path.is_file():
            raise PackageIntegrityError(
                "Existing package is incomplete; recover it before reuse"
            )
        manifest = json.loads(manifest_path.read_text())
        for filename, expected in manifest["files"].items():
            path = folder / filename
            if not path.is_file() or file_hash(path) != expected:
                raise PackageIntegrityError("An immutable package version changed")
        # Check cached output too before registering or reusing an attachment.
        for record in manifest["documents"]:
            if record["kind"] == "cover_letter.html":
                validate_cover_letter_html(
                    (folder / record["filename"]).read_text(), app
                )
        files, previews = [], []
        for record in manifest["documents"]:
            path = folder / record["filename"]
            existing = self.db.one(
                "SELECT * FROM artifacts WHERE path=%s", (str(path),)
            )
            if existing and existing["hash"] != record["hash"]:
                raise PackageIntegrityError("An immutable package artifact changed")
            aid = existing["id"] if existing else uid()
            self.db.execute(
                "INSERT INTO artifacts(id,application_id,path,hash,kind,claims) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(path) DO NOTHING",
                (
                    aid,
                    app["id"],
                    str(path),
                    record["hash"],
                    record["kind"],
                    js(record["claims"]),
                ),
            )
            relative = str(path.relative_to(root))
            item = {
                "id": aid,
                "path": str(path),
                "relative": relative,
                "hash": record["hash"],
                "kind": record["kind"],
                "pages": 1 if record["kind"].endswith(".pdf") else None,
            }
            if record["kind"].endswith(".png"):
                previews.append(item)
            else:
                files.append(item)
                Corpus(self.db).ingest(
                    relative
                    if app["candidate"] == "fixture"
                    else "_AgentGenerated/" + relative,
                    app["candidate"],
                    kind="generated_draft",
                )
        self.db.audit(
            "package.prepared",
            app["id"],
            {
                "artifacts": [f["id"] for f in files],
                "claim_fact_ids": all_ids,
                "renderer": VERSION,
            },
        )
        return {
            "files": files,
            "previews": previews,
            "review_path": str(folder / "Application_Review.html"),
            "version": version,
            "claim_fact_ids": all_ids,
            "renderer": VERSION,
            "checks": manifest["checks"],
            "inference_runs": run_ids,
        }
