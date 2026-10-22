from __future__ import annotations
from contextlib import asynccontextmanager
import hmac
import os
import json
import html
from pathlib import Path
from typing import Literal, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool
from .config import Settings, REPO, atomic_write
from .db import DB
from .queue import enqueue
from .workflow import Workflow, ApprovalInput, NotSubmittedInput
from .memory import Memory, FactInput
from .providers import Providers, ProviderConfig
from .search import SearchService, SearchRequest
from .hunt import Hunt, HuntRequest
from .jobspy_client import JobSpyClient
from .corpus import Corpus
from .tracker import Tracker, read_ods, TRACKER_NAME
from .fixtures import seed, assessment
from .verification import Verification
from .scoring import Assessment, shortlist
from .llm.contracts import InferenceRequest
from .llm.service import InferenceService
from .llm.routing import LLMConfig, DESTINATION, catalog
from .notifications import Notifications
from .requirement_decisions import RequirementDecisions, RequirementDecisionInput
from .answer_scopes import allows_global
from .package_review import PackageReview, PackageRevisionInput, PackageReviewInput
from .user_profiles import UserProfiles, ProfileInput, ProfileRoutingMiddleware


class TrackerUpdateInput(BaseModel):
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewed_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class TrackerModeInput(BaseModel):
    enabled: StrictBool


class SubmissionHandoffInput(BaseModel):
    reviewed_hash: str = Field(min_length=64, max_length=64)


class SelectionInput(BaseModel):
    conditional_reason: str | None = Field(default=None, max_length=2000)
    reviewed_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class RescoreInput(BaseModel):
    score_hash: str = Field(min_length=64, max_length=64)


class HandoffReviewInput(BaseModel):
    reviewed_hash: str = Field(min_length=64, max_length=64)
    confirm: Literal[True]


class ApplicationInput(BaseModel):
    employer: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    requisition: str = Field(min_length=1, max_length=100)
    track: Literal["software", "applied_ai", "solutions"] = "software"
    source: list[dict] | dict | None = None


class AnswerInput(BaseModel):
    context: str | None = Field(default=None, max_length=12000)
    value: Any
    scope: Literal["application", "global"] = "application"


class AnswerDraftInput(BaseModel):
    source_urls: list[str] = Field(default_factory=list, max_length=4)


class ResolveInput(BaseModel):
    key: str
    candidate: Literal["real", "fixture"] = "fixture"
    scope: str = "global"
    query: str | None = None


class RetrievalInput(BaseModel):
    query: str = Field(min_length=1, max_length=3000)
    candidate: Literal["real", "fixture"] = "fixture"
    kinds: list[str] | None = None
    full_document: bool = False
    rerank: bool = False


def create_app(settings=None, *, manage_profiles=True):
    settings = settings or Settings()
    db = DB(settings)
    flow = Workflow(db)
    user_profiles = UserProfiles(settings) if manage_profiles else None

    @asynccontextmanager
    async def lifespan(app):
        if user_profiles:
            user_profiles.initialize()
            settings.workspace = user_profiles.resolve("default").workspace
        else:
            db.migrate()
        yield

    app = FastAPI(
        title="Job Search Local API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.db = db

    if user_profiles:
        app.add_middleware(
            ProfileRoutingMiddleware,
            profiles=user_profiles,
            factory=lambda selected: create_app(selected, manage_profiles=False),
        )

        @app.get("/api/profiles")
        def list_user_profiles():
            return user_profiles.list()

        @app.post("/api/profiles", status_code=201)
        def create_user_profile(data: ProfileInput):
            return user_profiles.create(data)

    @app.middleware("http")
    async def security(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        allowed = {"127.0.0.1", "localhost", "testserver"}
        if host not in allowed:
            return JSONResponse({"detail": "Unrecognized local host"}, 400)
        protected = request.url.path.startswith("/api") or (
            request.url.path.startswith("/fixture")
            and request.method not in ("GET", "HEAD")
        )
        if protected:
            supplied = request.headers.get("x-jsa-token") or request.cookies.get(
                "jsa_session", ""
            )
            if not hmac.compare_digest(supplied, settings.token()):
                return JSONResponse(
                    {"detail": "Unlock this local workspace first"}, 401
                )
            if request.method not in ("GET", "HEAD"):
                origin = request.headers.get("origin")
                if origin and origin != settings.origin:
                    return JSONResponse(
                        {"detail": "Cross-origin changes are blocked"}, 403
                    )
                if request.headers.get("sec-fetch-site") == "cross-site":
                    return JSONResponse(
                        {"detail": "Cross-site changes are blocked"}, 403
                    )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers["Cache-Control"] = "no-store"
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
            )
        return response

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "mode": "local",
            "real_submission_enabled": settings.live_applications,
            "submission_mode": "external",
            "real_tracker_writes_enabled": settings.live_tracker_writes,
        }

    @app.get("/api/notifications")
    def notifications(
        candidate: Literal["real", "fixture"] = "real", include_resolved: bool = False
    ):
        return Notifications(db).snapshot(candidate, include_resolved)

    @app.post("/api/notifications/{nid}/read")
    def read_notification(nid: str, candidate: Literal["real", "fixture"] = "real"):
        return Notifications(db).mark_read(nid, candidate)

    @app.post("/api/session")
    def session():
        response = JSONResponse({"unlocked": True})
        response.set_cookie(
            "jsa_session",
            settings.token(),
            httponly=True,
            samesite="strict",
            max_age=28800,
        )
        return response

    @app.get("/api/overview")
    def overview(candidate: Literal["real", "fixture"] = "fixture"):
        snapshot = db.one(
            "SELECT * FROM tracker_snapshots WHERE candidate=%s ORDER BY created_at DESC LIMIT 1",
            (candidate,),
        )
        return {
            "candidate": candidate,
            "profile_id": settings.profile_id,
            "workspace": str(
                settings.workspace
                if candidate == "real"
                else settings.fixture_workspace
            ),
            "source_read_only": candidate == "real",
            "provider": Providers(db).config.model_dump(),
            "llm": InferenceService(db).config.model_dump(),
            "llm_destination": DESTINATION,
            "applications": [
                a | {"fit_review": RequirementDecisions(db).view(a)}
                for a in db.all(
                    "SELECT * FROM applications WHERE candidate=%s ORDER BY created_at DESC",
                    (candidate,),
                )
            ],
            "jobs": db.all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 30"),
            "facts": db.one(
                "SELECT count(*) AS count FROM facts WHERE candidate=%s AND active",
                (candidate,),
            )["count"],
            "documents": db.one(
                "SELECT count(*) AS count FROM documents WHERE candidate=%s AND NOT deleted",
                (candidate,),
            )["count"],
            "tracker": snapshot,
            "metrics": db.all("SELECT * FROM metrics ORDER BY name"),
            "conflicts": db.all("SELECT * FROM conflicts WHERE NOT resolved"),
            "real_submission_enabled": settings.live_applications,
            "real_tracker_writes_enabled": settings.live_tracker_writes,
            "notifications": Notifications(db).snapshot(candidate),
        }

    @app.post("/api/fixtures/seed")
    def seed_fixture():
        return seed(db)

    @app.post("/api/workspace/import")
    def import_workspace(candidate: Literal["real", "fixture"] = "real"):
        return enqueue(db, "import", {"candidate": candidate})

    @app.post("/api/workspace/ingest")
    def ingest_workspace(candidate: Literal["real", "fixture"] = "real"):
        return enqueue(db, "ingest", {"candidate": candidate})

    @app.get("/api/documents")
    def documents(candidate: Literal["real", "fixture"] = "real", offset: int = 0):
        return db.all(
            "SELECT d.*,v.status,v.warnings,v.hash,v.extracted_at FROM documents d LEFT JOIN versions v ON v.id=d.current_version WHERE candidate=%s ORDER BY path LIMIT 100 OFFSET %s",
            (candidate, max(0, offset)),
        )

    @app.post("/api/retrieve")
    def retrieve(data: RetrievalInput):
        return Corpus(db).retrieve(
            data.query,
            data.candidate,
            data.kinds,
            full_document=data.full_document,
            rerank=data.rerank,
        )

    @app.get("/api/facts")
    def facts(candidate: Literal["real", "fixture"] = "fixture"):
        return db.all(
            "SELECT * FROM facts WHERE candidate=%s ORDER BY active DESC,key,verified_at DESC",
            (candidate,),
        )

    @app.post("/api/facts")
    def save_fact(data: FactInput):
        if data.provenance.get("type") != "user_confirmation":
            raise ValueError("UI corrections must record user confirmation")
        return Memory(db).put(data)

    @app.post("/api/answers/resolve")
    def resolve(data: ResolveInput):
        return Memory(db).resolve(data.key, data.candidate, data.scope, data.query)

    @app.post("/api/hunts")
    def start_hunt(data: HuntRequest):
        return Hunt(db).start(data)

    @app.get("/api/hunts")
    def list_hunts(candidate: Literal["real", "fixture"] = "real"):
        return db.all(
            "SELECT * FROM hunts WHERE candidate=%s ORDER BY created_at DESC LIMIT 20",
            (candidate,),
        )

    @app.get("/api/hunts/{hid}")
    def get_hunt(hid: str):
        return Hunt(db).get(hid)

    @app.post("/api/hunts/{hid}/{action}")
    def control_hunt(hid: str, action: str):
        return Hunt(db).control(hid, action)

    @app.get("/api/search/profiles")
    def profiles():
        return SearchService(db).profiles()

    @app.get("/api/search/health")
    def source_health():
        return SearchService(db).health()

    @app.post("/api/search")
    async def search(data: SearchRequest):
        return await JobSpyClient(db).call(
            "start_search", {"request": data.model_dump()}
        )

    @app.get("/api/search/{jid}/results")
    def results(jid: str, offset: int = 0, limit: int = 20):
        return SearchService(db).results(jid, offset, limit)

    @app.get("/api/jobs/{jid}")
    def job(jid: str):
        row = db.one("SELECT * FROM jobs WHERE id=%s", (jid,))
        if not row:
            raise HTTPException(404)
        return row

    @app.post("/api/jobs/{jid}/cancel")
    def cancel(jid: str):
        row = db.one("SELECT * FROM jobs WHERE id=%s", (jid,))
        if row and row["kind"] == "hunt":
            Hunt(db).control(row["input"]["hunt_id"], "cancel")
        db.execute(
            "UPDATE jobs SET cancel_requested=true,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=%s",
            (jid,),
        )
        return {"cancel_requested": True}

    @app.post("/api/jobs/{jid}/resume")
    def resume_job(jid: str):
        row = db.one("SELECT * FROM jobs WHERE id=%s", (jid,))
        if not row or row["status"] not in ("failed", "cancelled"):
            raise ValueError("Only stopped jobs can resume")
        if row["kind"] == "hunt":
            return Hunt(db).control(row["input"]["hunt_id"], "resume")
        if row["kind"] == "submit" and db.one(
            "SELECT 1 FROM attempts WHERE application_id=%s",
            (row["input"]["application_id"],),
        ):
            raise ValueError(
                "Submission jobs with a recorded attempt require outcome reconciliation"
            )
        db.execute(
            "UPDATE jobs SET status='queued',cancel_requested=false,attempts=0,available_at=now() WHERE id=%s",
            (jid,),
        )
        return {"resumed": True}

    @app.get("/api/recommendations")
    def recommendations(
        candidate: Literal["real", "fixture"] = "fixture",
        track: Literal["software", "applied_ai", "solutions"] = "software",
    ):
        rows = db.all(
            "SELECT * FROM applications WHERE candidate=%s AND score->>'track'=%s",
            (candidate, track),
        )
        from datetime import datetime, timezone, timedelta

        for row in rows:
            verified = row["official"].get("verified_at")
            if not verified or datetime.fromisoformat(
                verified.replace("Z", "+00:00")
            ) < datetime.now(timezone.utc) - timedelta(hours=24):
                row["score"] = row["score"] | {
                    "primary": False,
                    "band": "conditional",
                    "reasons": row["score"].get("reasons", [])
                    + ["Official verification is stale"],
                }
        selected = shortlist(rows)
        return {
            "primary": selected,
            "screening_log": [r for r in rows if r not in selected],
            "maximum": 5,
        }

    @app.post("/api/applications")
    def create_application(data: ApplicationInput):
        return flow.create(**data.model_dump())

    @app.get("/api/applications/{aid}")
    def application(aid: str):
        result = flow.get(aid)
        result["submission_mode"] = "external"
        result["submission_recovery"] = (
            flow.submission_recovery(aid)
            if result["state"] == "unresolved"
            else {"can_retry": False}
        )
        result["fit_review"] = RequirementDecisions(db).view(result)
        from .selection import selection_review

        result["selection_review"] = selection_review(result)
        result["questions"] = db.all(
            "SELECT * FROM questions WHERE application_id=%s", (aid,)
        )
        fields = {f["key"]: f for f in result["payload"].get("form_schema", [])}
        for question in result["questions"]:
            from .form_policy import (
                answer_guidance,
                narrative_context_key,
                requires_explicit_answer,
            )

            field = fields.get(question["key"], {})
            question["can_suggest"] = bool(
                field.get("type") in ("text", "textarea")
                and not field.get("options")
                and field.get("purpose", "ordinary") == "ordinary"
                and not requires_explicit_answer(field)
                and (not question["answer_fact"] or question["needs_review"])
                and not result["paused"]
                and not result["imported"]
                and result["state"]
                in (
                    "missing_answers",
                    "review",
                    "prepared",
                    "selected",
                    "scored",
                    "manual_handoff",
                )
                and not db.one("SELECT 1 FROM attempts WHERE application_id=%s", (aid,))
            )
            if (
                question["can_suggest"]
                and question.get("reason", {}).get("code") == "personal_context_needed"
            ):
                question["reason"] = question["reason"] | {
                    "message": "Request a suggestion connecting your career history with this role, or write your own answer. Review the wording before confirming it."
                }
            guidance = answer_guidance(field) if field else None
            question["answer_guidance"] = guidance
            if guidance and guidance["choice_mismatch"]:
                context = Memory(db).resolve(
                    narrative_context_key(field),
                    result["candidate"],
                    aid,
                    retrieve=False,
                )
                question["answer_context"] = (
                    context.get("value", "")
                    if context.get("source") != "question"
                    else ""
                )
                question["reason"] = question.get("reason", {}) | {
                    "code": "narrative_choice_mismatch",
                    "message": guidance["message"],
                }
                question["reason"].pop("suggestion", None)
            context_suggestion = question.get("reason", {}).get("context_suggestion")
            if context_suggestion:
                from .form_answers import FormAnswers
                from .form_policy import narrative_field

                if (
                    not guidance
                    or not guidance["choice_mismatch"]
                    or not FormAnswers(db).valid(
                        result, narrative_field(field), context_suggestion, draft=True
                    )
                ):
                    question["reason"].pop("context_suggestion", None)
                    question["reason"]["context_message"] = (
                        "The evidence for the saved draft changed. Draft again or write your own examples."
                    )
            reusable = allows_global(question["key"], fields.get(question["key"]))
            question["allowed_scopes"] = (
                ["application", "global"] if reusable else ["application"]
            )
            question["scope_reason"] = (
                None
                if reusable
                else "This answer depends on the employer or role, so it can only be used for this application."
            )
            suggestion = question.get("reason", {}).get("suggestion")
            if suggestion:
                from .form_answers import FormAnswers

                if not FormAnswers(db).valid(
                    result, fields.get(question["key"], {}), suggestion, draft=True
                ):
                    question["reason"] = question["reason"] | {
                        "message": "Evidence changed after this draft was prepared. Prepare the answers again before using it."
                    }
                    question["reason"].pop("suggestion", None)
        result["receipt_history"] = db.all(
            "SELECT * FROM receipts WHERE application_id=%s", (aid,)
        )
        result["receipts"] = [
            r for r in result["receipt_history"] if not r["evidence"].get("invalidated")
        ]
        result["approvals"] = db.all(
            "SELECT * FROM approvals WHERE application_id=%s", (aid,)
        )
        result["attempts"] = db.all(
            "SELECT * FROM attempts WHERE application_id=%s", (aid,)
        )
        result["jobs"] = db.all(
            "SELECT * FROM jobs WHERE input->>'application_id'=%s ORDER BY created_at DESC LIMIT 30",
            (aid,),
        )
        from .attention_summary import discard_blocker

        result["discard_blocker"] = discard_blocker(
            result,
            bool(result["attempts"]),
            bool(result["receipts"]),
            bool(
                db.one(
                    "SELECT 1 FROM jobs WHERE input->>'application_id'=%s AND status='running'",
                    (aid,),
                )
            ),
        )
        result["can_discard"] = result["discard_blocker"] is None
        result["package_review"] = PackageReview(db).status(result)
        result["handoff_ready"] = (
            settings.state / "handoffs" / (aid + ".html")
        ).is_file()
        result["artifacts"] = db.all(
            "SELECT * FROM artifacts WHERE application_id=%s ORDER BY kind", (aid,)
        )
        for artifact in result["artifacts"]:
            # Legacy artifact rows have no creation timestamp. Generated versions
            # are immutable, so file modification time also orders those records.
            path = Path(artifact["path"]).resolve()
            artifact["modified_at"] = None
            if any(
                path.is_relative_to(root.resolve())
                for root in (
                    settings.fixture_workspace,
                    settings.state / "prepared-workspace",
                )
            ):
                try:
                    artifact["modified_at"] = path.stat().st_mtime
                except OSError:
                    pass
        from .progress import progress

        try:
            result["review"] = flow.review(aid)
            result["review_error"] = None
            current_hash = result["review"]["hash"]
        except ValueError as exc:
            result["review"] = None
            result["review_error"] = str(exc)
            current_hash = None
        result["progress"] = progress(result, current_hash, result["review_error"])
        return result

    @app.post("/api/applications/{aid}/refresh-opening")
    def refresh_opening(aid: str):
        from .progress import ACTIVE

        a = flow.get(aid)
        flow.editable(a)
        if (
            a["paused"]
            or a["state"] not in ACTIVE
            or db.one("SELECT 1 FROM attempts WHERE application_id=%s", (aid,))
        ):
            raise ValueError(
                "Opening checks are available before submission on active applications"
            )
        return enqueue(db, "refresh_opening", {"application_id": aid}, max_attempts=2)

    @app.post("/api/applications/{aid}/evaluate")
    def evaluate_application(aid: str):
        a = flow.get(aid)
        flow.editable(a)
        if a["state"] != "verified" or a["paused"]:
            raise ValueError(
                "Verify the opening first; use Rescore for an existing assessment"
            )
        return enqueue(db, "rescore", {"application_id": aid}, max_attempts=2)

    @app.post("/api/applications/{aid}/verify")
    def verify(aid: str, data: dict):
        a = flow.get(aid)
        evidence = Verification(db).verify(
            a,
            data.get("url", settings.origin + "/fixture/posting/" + aid),
            data.get("official_host", ""),
        )
        return flow.set_official(aid, evidence)

    @app.post("/api/applications/{aid}/score")
    def score(aid: str, data: Assessment):
        return flow.score(aid, data)

    @app.post("/api/applications/{aid}/requirements/decision")
    def requirement_decision(aid: str, data: RequirementDecisionInput):
        RequirementDecisions(db).decide(aid, data)
        return application(aid)

    @app.post("/api/applications/{aid}/rescore")
    def rescore(aid: str, data: RescoreInput):
        from .config import digest

        a = flow.get(aid)
        flow.editable(a)
        if a["paused"] or a["state"] not in (
            "scored",
            "selected",
            "prepared",
            "missing_answers",
            "review",
            "verified",
            "manual_handoff",
        ):
            raise ValueError(
                "Rescoring requires an active application before selection or submission"
            )
        if db.one("SELECT 1 FROM attempts WHERE application_id=%s", (aid,)):
            raise ValueError(
                "Attempted applications cannot change fit; reconcile the outcome first"
            )
        if digest(a["score"]) != data.score_hash:
            raise ValueError("The assessment changed. Refresh it before rescoring")
        service = InferenceService(db)
        if service.config.backend == "codex_exec" and not service.config.enabled:
            raise ValueError("Enable the Codex data destination before rescoring")
        return enqueue(db, "rescore", {"application_id": aid}, max_attempts=2)

    @app.post("/api/applications/{aid}/fixture-score")
    def fixture_score(aid: str):
        a = flow.get(aid)
        if a["candidate"] != "fixture":
            raise ValueError("Fixture scores only")
        return flow.score(
            aid,
            assessment(
                db,
                a["payload"].get("track", "software"),
                a["employer"] == "Harbor Demo",
            ),
        )

    @app.post("/api/applications/{aid}/select")
    def select(aid: str, data: SelectionInput):
        return flow.select(aid, data.conditional_reason, data.reviewed_hash)

    @app.post("/api/applications/{aid}/prepare")
    def prepare(aid: str):
        a = flow.get(aid)
        flow.editable(a)
        if a["paused"] or a["state"] not in (
            "selected",
            "preparing",
            "prepared",
            "review",
        ):
            raise ValueError("Select this role before preparing documents")
        if a["payload"].get("package", {}).get("version"):
            raise ValueError(
                "Documents already exist. Continue with them or request a document revision"
            )
        return enqueue(db, "package", {"application_id": aid})

    @app.post("/api/applications/{aid}/package-revisions")
    def revise_package(aid: str, data: PackageRevisionInput):
        return PackageReview(db).revise(aid, data)

    @app.post("/api/applications/{aid}/package-review")
    def review_package(aid: str, data: PackageReviewInput):
        return PackageReview(db).accept(aid, data)

    @app.patch("/api/applications/{aid}/payload")
    def patch(aid: str, data: dict):
        if set(data) - {"attachments", "consents", "scenario"}:
            raise ValueError("Use confirmed-answer controls to change answers")
        if "scenario" in data and data["scenario"] not in (
            "normal",
            "challenge",
            "no_receipt",
            "bot_rejected",
            "new_requirement",
        ):
            raise ValueError("Unknown fixture scenario")
        return flow.update_payload(aid, data)

    @app.post("/api/applications/{aid}/fill")
    @app.post("/api/applications/{aid}/continue")
    def fill(aid: str):
        flow.continue_prepared(aid)
        return enqueue(db, "fill", {"application_id": aid}, max_attempts=1)

    @app.get("/api/applications/{aid}/submission-handoff")
    def read_submission_handoff(aid: str):
        from .submission_handoff import SubmissionHandoff

        return SubmissionHandoff(db).latest(aid)

    @app.post("/api/applications/{aid}/submission-handoff")
    def export_submission_handoff(aid: str, data: SubmissionHandoffInput):
        from .submission_handoff import SubmissionHandoff

        return SubmissionHandoff(db).export(aid, data.reviewed_hash)

    @app.get("/api/applications/{aid}/review")
    def review(aid: str):
        return flow.review(aid)

    @app.post("/api/applications/{aid}/approve")
    def approve(aid: str, data: ApprovalInput):
        return flow.authorize(aid, data)

    @app.post("/api/applications/{aid}/resume-approved")
    def resume_approved(aid: str):
        a = flow.get(aid)
        flow.editable(a)
        if (
            a["paused"]
            or a["state"] != "authorized"
            or db.one("SELECT 1 FROM attempts WHERE application_id=%s", (aid,))
        ):
            raise ValueError(
                "Only an approved application with no sending attempt can resume here"
            )
        review = flow.review(aid)
        if review["questions"] or not db.one(
            "SELECT 1 FROM approvals WHERE application_id=%s AND payload_hash=%s AND NOT invalidated",
            (aid, review["hash"]),
        ):
            raise ValueError(
                "The saved approval no longer matches; review this application again"
            )
        return enqueue(db, "submit", {"application_id": aid}, max_attempts=1)

    @app.post("/api/applications/{aid}/retry-submission")
    def retry_submission(aid: str):
        return flow.retry_submission(aid)

    @app.post("/api/applications/{aid}/confirm-not-submitted")
    def confirm_not_submitted(aid: str, data: NotSubmittedInput):
        return flow.confirm_not_submitted(aid, data)

    @app.post("/api/applications/{aid}/review-answer")
    def review_answer(aid: str, data: dict):
        a = flow.get(aid)
        flow.editable(a)
        if a["state"] not in (
            "scored",
            "selected",
            "review",
            "missing_answers",
            "prepared",
            "authorized",
            "manual_handoff",
        ):
            raise ValueError(
                "Answer corrections require an application that has not been attempted"
            )
        if db.one("SELECT 1 FROM attempts WHERE application_id=%s", (aid,)):
            raise ValueError("Reconcile the submission outcome before changing answers")
        field = next(
            (
                f
                for f in a["payload"].get("form_schema", [])
                if f["key"] == data.get("key") and f.get("name") and f["type"] != "file"
            ),
            None,
        )
        if not field:
            raise ValueError("Choose a field from the inspected employer form")
        q = flow.ask(
            aid,
            field["key"],
            field["label"],
            {
                "reason": {
                    "code": "user_requested_correction",
                    "message": "Edit the prepared answer below, then confirm it to save your correction.",
                }
            },
        )
        db.execute("UPDATE questions SET needs_review=true WHERE id=%s", (q["id"],))
        flow.update_payload(aid, {"browser_hash": None})
        return {
            "question_id": q["id"],
            "key": field["key"],
            "current_value": a["payload"]
            .get("answers", {})
            .get(field["key"], {})
            .get("value"),
        }

    @app.post("/api/questions/{qid}/draft-context")
    def draft_question_context(qid: str):
        from .form_answers import FormAnswers

        q = db.one("SELECT application_id FROM questions WHERE id=%s", (qid,))
        if not q:
            raise ValueError("Question not found")
        FormAnswers(db).context_question(q["application_id"], qid)
        return enqueue(
            db,
            "draft_answer_context",
            {"application_id": q["application_id"], "question_id": qid},
            max_attempts=1,
        )

    @app.post("/api/questions/{qid}/draft-answer")
    def draft_answer(qid: str, data: AnswerDraftInput):
        from .form_answers import FormAnswers

        q = db.one("SELECT application_id FROM questions WHERE id=%s", (qid,))
        if not q:
            raise ValueError("Question not found")
        FormAnswers(db).context_question(q["application_id"], qid, answer=True)
        return enqueue(
            db,
            "draft_form_answer",
            {
                "application_id": q["application_id"],
                "question_id": qid,
                "source_urls": data.source_urls,
            },
            max_attempts=1,
        )

    @app.post("/api/questions/{qid}/answer")
    def answer(qid: str, data: AnswerInput):
        return flow.answer(qid, data.value, data.scope, context=data.context)

    @app.post("/api/applications/{aid}/control")
    def control(aid: str, data: dict):
        return flow.control(aid, data["action"], data.get("reason", ""))

    @app.post("/api/applications/{aid}/reconcile")
    def reconcile(aid: str):
        return enqueue(db, "reconcile", {"application_id": aid}, max_attempts=1)

    @app.post("/api/applications/{aid}/continue-verification")
    def continue_verification(aid: str):
        application = flow.get(aid)
        flow.editable(application)
        if application["state"] != "awaiting_challenge" or application["paused"]:
            raise ValueError("No active employer verification is waiting")
        if (
            application["payload"].get("form_plan", {}).get("spec", {}).get("adapter")
            != "greenhouse"
        ):
            raise ValueError("This employer requires browser takeover")
        return enqueue(
            db, "continue_verification", {"application_id": aid}, max_attempts=1
        )

    @app.post("/api/applications/{aid}/confirm-receipt")
    def confirm_receipt(aid: str, data: dict):
        if data.get("confirm") is not True or not data.get("note"):
            raise ValueError("Explicit receipt confirmation and a note are required")
        a = flow.get(aid)
        attempt = db.one(
            "SELECT id FROM attempts WHERE application_id=%s ORDER BY started_at DESC",
            (aid,),
        )
        return flow.record_receipt(
            aid,
            attempt["id"] if attempt else None,
            {
                "confirmed": True,
                "text": data["note"],
                "requisition": a["requisition"],
                "source": "explicit user confirmation",
            },
            "user_confirmation",
        )

    @app.post("/api/applications/{aid}/handoff")
    def handoff(aid: str):
        from .browser import BrowserWorker

        flow.editable(flow.get(aid))
        return BrowserWorker(db).handoff(
            aid, "Manual browser handoff requested by the user"
        )

    @app.post("/api/applications/{aid}/handoff-review")
    def handoff_review(aid: str, data: HandoffReviewInput):
        from .browser import BrowserWorker

        result = flow.review_handoff(aid, data.reviewed_hash)
        BrowserWorker(db).handoff(
            aid,
            "Prepared package reviewed. Complete the employer form and final review in your browser, then record the employer receipt.",
        )
        return result

    @app.get("/api/handoffs/{aid}")
    def read_handoff(aid: str):
        flow.editable(flow.get(aid))
        path = settings.state / "handoffs" / (aid + ".html")
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(
            path,
            media_type="text/html",
            headers={
                "Content-Security-Policy": "sandbox allow-popups; default-src 'none'; style-src 'unsafe-inline'"
            },
        )

    @app.get("/api/artifacts/{artifact_id}")
    def artifact(artifact_id: str, preview: bool = False):
        a = db.one("SELECT * FROM artifacts WHERE id=%s", (artifact_id,))
        if not a:
            raise HTTPException(404)
        path = Path(a["path"]).resolve()
        if not any(
            path.is_relative_to(root.resolve())
            for root in (
                settings.fixture_workspace,
                settings.state / "prepared-workspace",
            )
        ):
            raise HTTPException(403)
        headers = {
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'"
        }
        return FileResponse(
            path,
            filename=None if preview else path.name,
            headers=headers,
            content_disposition_type="inline" if preview else "attachment",
        )

    @app.get("/api/settings")
    def get_settings():
        return {
            "provider": Providers(db).config.model_dump(),
            "llm": InferenceService(db).config.model_dump(),
            "llm_destination": DESTINATION,
            "workspace": str(settings.workspace),
            "state": str(settings.state),
            "live_search": settings.live_search or db.setting("live_search", False),
            "real_submission_enabled": settings.live_applications,
            "real_tracker_writes_enabled": settings.live_tracker_writes,
            "official_hosts": db.setting("official_hosts", []),
        }

    @app.put("/api/settings/llm")
    def configure_llm(data: LLMConfig):
        InferenceService(db).configure(data)
        return data

    @app.get("/api/llm/catalog")
    def llm_catalog():
        return catalog()

    @app.post("/api/llm/jobs")
    def llm_job(data: InferenceRequest):
        return InferenceService(db).start(data)

    @app.get("/api/llm/runs")
    def llm_runs(candidate: Literal["fixture", "real"] = "fixture"):
        return db.all(
            "SELECT id,job_id,task,application_id,model,effort,backend,status,error,usage,created_at,updated_at FROM llm_runs WHERE candidate=%s ORDER BY created_at DESC LIMIT 30",
            (candidate,),
        )

    @app.get("/api/llm/runs/{rid}")
    def llm_run(rid: str):
        row = db.one("SELECT * FROM llm_runs WHERE id=%s", (rid,))
        if not row:
            raise HTTPException(404)
        row.pop("directory", None)
        row.pop("pid", None)
        row["events"] = db.all(
            "SELECT seq,at,event,detail FROM llm_events WHERE run_id=%s ORDER BY seq",
            (rid,),
        )
        return row

    @app.post("/api/llm/runs/{rid}/apply-score")
    def apply_llm_score(rid: str):
        return InferenceService(db).apply_score(rid)

    @app.put("/api/settings/provider")
    def provider(data: ProviderConfig):
        Providers(db).configure(data)
        return data

    @app.post("/api/settings/official-host")
    def official_host(data: dict):
        host = data["host"]
        if not data.get("confirmed") or "/" in host or ":" in host:
            raise ValueError("Confirm a hostname without a URL path")
        hosts = db.setting("official_hosts", [])
        if host not in hosts:
            hosts.append(host)
        db.set_setting("official_hosts", hosts)
        return {"official_hosts": hosts}

    @app.get("/api/tracker")
    def tracker(candidate: Literal["real", "fixture"] = "real"):
        root = settings.workspace if candidate == "real" else settings.fixture_workspace
        return read_ods(root / TRACKER_NAME)

    @app.get("/api/tracker/metrics")
    def tracker_metrics(candidate: Literal["real", "fixture"] = "real"):
        from .tracker_metrics import dashboard

        return dashboard(db, candidate)

    @app.get("/api/tracker/preview/{aid}")
    def tracker_preview(aid: str):
        return Tracker(db).preview(aid)

    @app.put("/api/settings/tracker-writes")
    def tracker_mode(data: TrackerModeInput):
        if os.environ.get("JSA_LIVE_TRACKER_WRITES") is not None:
            raise ValueError(
                "Tracker writes are controlled by JSA_LIVE_TRACKER_WRITES; change that configuration and restart the API."
            )
        atomic_write(
            settings.state / "tracker-settings.json",
            json.dumps({"enabled": data.enabled}),
        )
        settings.live_tracker_writes = data.enabled
        db.audit("tracker_mode_changed", "deployment", {"enabled": data.enabled})
        return {"enabled": data.enabled, "workspace": str(settings.workspace)}

    @app.post("/api/tracker/update/{aid}")
    def tracker_update(aid: str, data: TrackerUpdateInput):
        return Tracker(db).update(aid, data.expected_hash, data.reviewed_hash)

    @app.post("/api/tracker/conflicts/{cid}/resolve")
    def resolve_conflict(cid: str, data: dict):
        return Tracker(db).resolve_import_conflict(cid, data["choice"])

    @app.post("/api/tracker/recover")
    def recover_tracker(candidate: Literal["real", "fixture"] = "fixture"):
        return Tracker(db).recover(candidate)

    @app.post("/api/reports/manual")
    def manual_report(candidate: Literal["real", "fixture"] = "fixture"):
        return Tracker(db).manual_report(candidate, on_demand=True)

    @app.get("/api/audit")
    def audit():
        return db.all("SELECT * FROM audit ORDER BY seq DESC LIMIT 200")

    @app.get("/fixture/posting/{aid}", response_class=HTMLResponse)
    def posting(aid: str):
        a = flow.get(aid)
        if a["candidate"] != "fixture":
            raise HTTPException(404)
        return f'<!doctype html><meta charset="utf-8"><h1>{html.escape(a["role"])}</h1><p>Synthetic employer: {html.escape(a["employer"])}. Requisition {html.escape(a["requisition"])}. Ohio eligible. Annual base $110,000 to $140,000. C# APIs, production delivery, code review and support workflows.</p><a href="/fixture/application/{aid}">Apply for this role</a>'

    @app.get("/fixture/application/{aid}", response_class=HTMLResponse)
    def fixture_form(aid: str):
        a = flow.get(aid)
        if a["candidate"] != "fixture":
            raise HTTPException(404)
        scenario = a["payload"].get("scenario", "normal")
        challenge = (
            '<section data-challenge><h2>Verification required</h2><p>Fixture challenge. Complete this step yourself; no code is saved.</p><input type="password" autocomplete="off" aria-label="Ephemeral fixture code"><button type="button" id="challenge-done">I completed verification</button></section>'
            if scenario == "challenge"
            else ""
        )
        qualification = (
            '<label>Production agent tenure<input name="production_agent_years" data-key="production_agent_years" data-requirement="Two years of production agent delivery" data-category="technical" required></label>'
            if scenario == "new_requirement"
            else ""
        )
        rejected = (
            "<p data-bot-rejected>This fixture rejects the controlled browser. Use a normal-browser handoff.</p>"
            if scenario == "bot_rejected"
            else ""
        )
        return f'''<!doctype html><html><head><meta charset="utf-8"><title>Fixture application</title><style>body{{font:17px system-ui;max-width:720px;margin:35px auto;background:#f3f6f5;color:#172f37}}label{{display:block;margin:20px 0}}input,select{{display:block;padding:10px;width:95%;font:inherit}}button{{padding:14px;background:#0d655b;color:white;border:0;border-radius:6px}}.notice{{padding:15px;background:#fff0d0}}</style></head><body><p class="notice">Local demonstration. No real application will be sent.</p><h1>{html.escape(a["employer"])}</h1><h2>{html.escape(a["role"])}</h2>{challenge}{rejected}<form id="application" data-application="{aid}">{qualification}<label>Professional name<input name="professional_name" data-key="professional_name" required></label><label>Email<input name="email" data-key="email" type="email" required></label><label>Annual base salary target<input name="salary" data-key="salary" required></label><label>Travel commitment for this role<select name="travel" data-key="travel" required><option value="">Choose</option><option value="Up to 10%">Up to 10%</option><option value="No travel">No travel</option></select></label><label>Resume and optional cover letter<input name="attachments" type="file" multiple required></label><p>This final button sends the fixture application. Approve the matching review in the job-search UI first.</p><button type="submit">Submit application</button></form><div id="outcome" role="status"></div><script src="/fixture/form.js"></script></body></html>'''

    @app.get("/fixture/form.js")
    def fixture_script():
        return Response(
            """document.querySelector('#challenge-done')?.addEventListener('click',()=>document.querySelector('[data-challenge]').remove());
const form=document.querySelector('#application'); form?.addEventListener('submit',async e=>{e.preventDefault();const values={}; for(const el of form.querySelectorAll('[data-key]'))values[el.dataset.key]=el.value;
const r=await fetch('/fixture/submit/'+form.dataset.application,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answers:values})}); const data=await r.json(); const out=document.querySelector('#outcome');out.textContent=data.message||data.detail;if(data.received){out.dataset.receipt='true';out.dataset.requisition=data.requisition;}});""",
            media_type="application/javascript",
        )

    @app.post("/fixture/submit/{aid}")
    def fixture_submit(aid: str, data: dict):
        a = flow.get(aid)
        if a["candidate"] != "fixture" or a["state"] not in (
            "submission_attempt",
            "receipt_verification",
        ):
            raise ValueError("Fixture submission requires a durable authorized attempt")
        attempt = db.one(
            "SELECT * FROM attempts WHERE application_id=%s AND state='started'", (aid,)
        )
        if not attempt:
            raise ValueError("No authorized attempt")
        review = flow.review(aid)
        if attempt["payload_hash"] != review["hash"]:
            raise ValueError("Payload changed after authorization")
        for k, answer in review["payload"]["answers"].items():
            if k in ("professional_name", "email", "salary", "travel") and str(
                answer["value"]
            ) != data.get("answers", {}).get(k):
                raise ValueError("Fixture form does not match approved answers")
        if a["payload"].get("scenario") == "no_receipt":
            return {
                "received": False,
                "message": "The connection ended without a receipt.",
            }
        return {
            "received": True,
            "requisition": a["requisition"],
            "message": f"Application received by {a['employer']} for {a['requisition']}. This is a local fixture receipt.",
        }

    from .fixture_portal import register_portal_fixtures

    register_portal_fixtures(app, db)
    from .greenhouse_fixture import register_greenhouse_fixture

    register_greenhouse_fixture(app, db)
    dist = REPO / "web/dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    return app


app = create_app()
