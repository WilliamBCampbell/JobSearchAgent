from __future__ import annotations
from enum import StrEnum
from pathlib import Path
import json
from pydantic import BaseModel, Field
from typing import Literal
from .config import digest, file_hash, atomic_write, POLICY
from .db import uid, js
from .memory import Memory, FactInput
from .answer_scopes import allows_global, fact_allows_global
from .scoring import duplicate_groups, Assessment


class State(StrEnum):
    DISCOVERED = "discovered"
    VERIFIED = "verified"
    SCORED = "scored"
    SELECTED = "selected"
    PREPARING = "preparing"
    PREPARED = "prepared"
    FILLING = "filling"
    MISSING = "missing_answers"
    REVIEW = "review"
    AUTHORIZED = "authorized"
    ATTEMPT = "submission_attempt"
    RECEIPT = "receipt_verification"
    SUBMITTED = "submitted"
    UNRESOLVED = "unresolved"
    CHALLENGE = "awaiting_challenge"
    DEFERRED = "deferred"
    CLOSED = "closed"
    SKIPPED = "skipped"
    FOLLOWUP = "follow_up"
    HANDOFF = "manual_handoff"
    REJECTED = "rejected"


TRANSITIONS = {
    "discovered": {"verified", "closed", "skipped"},
    "verified": {"scored", "closed"},
    "scored": {"selected", "skipped", "deferred", "preparing"},
    "selected": {"preparing", "deferred"},
    "preparing": {"prepared", "selected"},
    "prepared": {"filling", "deferred", "preparing"},
    "filling": {
        "missing_answers",
        "review",
        "awaiting_challenge",
        "manual_handoff",
        "scored",
    },
    "missing_answers": {"filling", "deferred", "preparing"},
    "review": {"authorized", "filling", "deferred", "scored", "preparing"},
    "authorized": {
        "submission_attempt",
        "review",
        "awaiting_challenge",
        "manual_handoff",
        "preparing",
    },
    "submission_attempt": {"receipt_verification", "unresolved", "awaiting_challenge"},
    "receipt_verification": {"submitted", "unresolved", "awaiting_challenge"},
    "awaiting_challenge": {
        "filling",
        "review",
        "authorized",
        "receipt_verification",
        "unresolved",
        "manual_handoff",
    },
    "unresolved": {"submitted", "manual_handoff", "deferred"},
    "manual_handoff": {"submitted", "deferred", "filling", "preparing", "scored"},
    "deferred": {"selected", "filling", "manual_handoff"},
    "submitted": {"follow_up", "rejected"},
    "follow_up": {"submitted", "rejected"},
    "closed": set(),
    "skipped": set(),
    "rejected": set(),
}


class ApprovalInput(BaseModel):
    reviewed_hash: str = Field(min_length=64, max_length=64)
    approve: Literal[True]


class NotSubmittedInput(BaseModel):
    confirm: Literal[True]
    attempt_id: str
    run_id: str
    reviewed_hash: str = Field(min_length=64, max_length=64)


class Workflow:
    def __init__(self, db):
        self.db = db

    def get(self, aid, conn=None):
        row = (
            conn.execute(
                "SELECT * FROM applications WHERE id=%s FOR UPDATE", (aid,)
            ).fetchone()
            if conn
            else self.db.one("SELECT * FROM applications WHERE id=%s", (aid,))
        )
        if not row:
            raise ValueError("Application not found")
        return row

    def editable(self, app):
        if app["imported"]:
            raise ValueError(
                "Imported application records are read-only during development"
            )

    def create(
        self,
        employer,
        role,
        requisition,
        track="software",
        source=None,
        candidate="fixture",
        discovery_key=None,
    ):
        if candidate not in ("real", "fixture"):
            raise ValueError("Unknown candidate")
        if discovery_key:
            old = self.db.one(
                "SELECT * FROM applications WHERE candidate=%s AND payload->>'discovery_key'=%s",
                (candidate, discovery_key),
            )
            if old:
                return old
        prior = self.db.all(
            "SELECT * FROM applications WHERE candidate=%s", (candidate,)
        )
        duplicates = duplicate_groups(
            [{"company": employer, "title": role, "requisition": requisition}], prior
        )[0]
        if duplicates["duplicate_review"]:
            raise ValueError("Prior application or ambiguous duplicate requires review")
        aid = uid()
        self.db.execute(
            "INSERT INTO applications(id,candidate,employer,role,requisition,state,payload) VALUES(%s,%s,%s,%s,%s,'discovered',%s)",
            (
                aid,
                candidate,
                employer,
                role,
                requisition,
                js(
                    {
                        "track": track,
                        "discovery_key": discovery_key,
                        "sources": source
                        if isinstance(source, list)
                        else [source]
                        if source
                        else [],
                        "answers": {},
                        "attachments": [],
                        "consents": {},
                        "policy": POLICY,
                    }
                ),
            ),
        )
        self.db.audit("application.created", aid)
        return self.get(aid)

    def transition(self, aid, state, detail=None, conn=None):
        if conn is None:
            with self.db.tx() as c:
                return self.transition(aid, state, detail, c)
        app = self.get(aid, conn)
        self.editable(app)
        if app["paused"]:
            raise ValueError("Application is paused")
        if state not in TRANSITIONS[app["state"]]:
            raise ValueError(f"Cannot move from {app['state']} to {state}")
        if (
            state in ("filling", "scored")
            and conn.execute(
                "SELECT 1 FROM attempts WHERE application_id=%s", (aid,)
            ).fetchone()
        ):
            raise ValueError(
                "Reconcile the existing submission attempt before filling or changing fit"
            )
        conn.execute(
            "UPDATE applications SET state=%s,revision=revision+1,updated_at=now() WHERE id=%s",
            (state, aid),
        )
        if state in (
            "review",
            "scored",
            "manual_handoff",
            "deferred",
            "closed",
            "skipped",
        ):
            conn.execute(
                "UPDATE followups SET resolved=true WHERE id=%s",
                (digest([aid, "answer_handoff"]),),
            )
        self.db.audit("workflow." + state, aid, detail, conn)
        return state

    def set_official(self, aid, evidence):
        with self.db.tx() as c:
            app = self.get(aid, c)
            self.editable(app)
            if c.execute(
                "SELECT 1 FROM attempts WHERE application_id=%s", (aid,)
            ).fetchone():
                raise ValueError(
                    "Reconcile the submission outcome before changing the official opening"
                )
            c.execute(
                "UPDATE applications SET official=%s,revision=revision+1,updated_at=now() WHERE id=%s",
                (js(evidence), aid),
            )
            c.execute(
                "UPDATE approvals SET invalidated=true WHERE application_id=%s", (aid,)
            )
            if app["score"].get("scores") and evidence.get("source_hash") != app[
                "official"
            ].get("source_hash"):
                c.execute(
                    'UPDATE applications SET score=score || \'{"evidence_changed":true,"primary":false}\'::jsonb WHERE id=%s',
                    (aid,),
                )
            if evidence.get("open") and app["state"] == "discovered":
                self.transition(aid, "verified", conn=c)
            elif (
                evidence.get("status") == "closed"
                and "closed" in TRANSITIONS[app["state"]]
            ):
                self.transition(aid, "closed", conn=c)
        return self.get(aid)

    def score(self, aid, assessment: Assessment):
        app = self.get(aid)
        self.editable(app)
        from .requirement_decisions import RequirementDecisions

        with self.db.tx() as c:
            app = self.get(aid, c)
            self.editable(app)
            if app["paused"]:
                raise ValueError("Application is paused")
            if app["state"] not in (
                "verified",
                "scored",
                "selected",
                "prepared",
                "missing_answers",
                "review",
                "filling",
                "manual_handoff",
            ):
                raise ValueError("Fit can change only before submission")
            if c.execute(
                "SELECT 1 FROM attempts WHERE application_id=%s", (aid,)
            ).fetchone():
                raise ValueError("Reconcile the submission outcome before changing fit")
            confirmed = {
                f["id"]
                for f in c.execute(
                    "SELECT id FROM facts WHERE candidate=%s AND active AND status='confirmed' AND scope=ANY(%s)",
                    (app["candidate"], ["global", aid]),
                ).fetchall()
            }
            score = RequirementDecisions(self.db).evaluate(
                app, assessment, confirmed, c
            )
            if not app["score"].get("scores"):
                self.db.audit(
                    "fit.initial_score",
                    aid,
                    {"total": score["total"], "band": score["band"]},
                    c,
                )
            c.execute(
                "UPDATE applications SET score=%s,revision=revision+1,updated_at=now() WHERE id=%s",
                (js(score), aid),
            )
            # A reevaluation changes fit, not completed documents or answers.
            # New form requirements deliberately return filling to fit review.
            if app["state"] in ("verified", "filling"):
                self.transition(aid, "scored", conn=c)
            c.execute(
                "UPDATE approvals SET invalidated=true WHERE application_id=%s", (aid,)
            )
        return self.get(aid)

    def select(self, aid, conditional_reason=None, reviewed_hash=None):
        from .selection import selection_review

        with self.db.tx() as c:
            app = self.get(aid, c)
            self.editable(app)
            review = selection_review(app)
            if reviewed_hash is not None and reviewed_hash != review["review_hash"]:
                raise ValueError(
                    "The role assessment changed. Review the current selection details before continuing."
                )
            if review["mode"] == "blocked":
                raise ValueError(" ".join(review["blockers"]))
            if conditional_reason is not None and not isinstance(
                conditional_reason, str
            ):
                raise ValueError(
                    "The reason for pursuing a conditional role must be text"
                )
            reason = (conditional_reason or "").strip()
            if len(reason) > 2000:
                raise ValueError("Keep the selection reason under 2,000 characters")
            if review["mode"] == "conditional" and not reason:
                raise ValueError(
                    "Conditional roles need an explicit reason for pursuing them"
                )
            self.transition(
                aid,
                "selected",
                {
                    "conditional_reason": reason or None,
                    "selection_review_hash": review["review_hash"],
                },
                c,
            )
        return self.get(aid)

    def continue_prepared(self, aid):
        """Resume a selected package without rewriting its reviewed files."""
        from .progress import document_review_needed, opening_current

        with self.db.tx() as c:
            app = self.get(aid, c)
            self.editable(app)
            if (
                app["paused"]
                or c.execute(
                    "SELECT 1 FROM attempts WHERE application_id=%s", (aid,)
                ).fetchone()
            ):
                raise ValueError(
                    "Resume paused work or reconcile the submission outcome first"
                )
            if not app["payload"].get("package"):
                raise ValueError("Prepare application documents first")
            if document_review_needed(app):
                raise ValueError(
                    "Review the current documents before preparing the employer application"
                )
            if not opening_current(app["official"]):
                raise ValueError("Verify the official opening before continuing")
            if not app["score"].get("primary") or app["score"].get("evidence_changed"):
                raise ValueError("Resolve the fit checks and rescore before continuing")
            if app["state"] == "selected":
                # These checkpoints represent reuse, not a new generation run.
                self.review_payload(app | {"payload": app["payload"] | {"answers": {}}})
                self.transition(aid, "preparing", {"reuse_existing_package": True}, c)
                self.transition(aid, "prepared", conn=c)
            elif app["state"] not in {
                "prepared",
                "missing_answers",
                "review",
                "filling",
                "manual_handoff",
                "awaiting_challenge",
            }:
                raise ValueError(
                    "Complete the role decision before continuing with this package"
                )
        return self.get(aid)

    def review_payload(self, app):
        p = app["payload"]
        attachments = []
        for artifact_id in p.get("attachments", []):
            row = self.db.one(
                "SELECT * FROM artifacts WHERE id=%s AND application_id=%s",
                (artifact_id, app["id"]),
            )
            if (
                not row
                or not Path(row["path"]).is_file()
                or file_hash(Path(row["path"])) != row["hash"]
            ):
                raise ValueError("An attachment is missing or has changed")
            if not Memory(self.db)._valid({"fact_ids": row["claims"]}):
                raise ValueError("An attached document contains stale evidence")
            attachments.append(
                {
                    "id": row["id"],
                    "name": Path(row["path"]).name,
                    "hash": row["hash"],
                    "kind": row["kind"],
                }
            )
        answers = {}
        fields = {f["key"]: f for f in p.get("form_schema", [])}
        for key, answer in p.get("answers", {}).items():
            derived = answer.get("source") in ("form_evidence", "profile_memory")
            if answer.get("source") == "profile_memory":
                from .profile_answers import ProfileAnswers

                if key not in fields or not ProfileAnswers(self.db).valid(
                    app, fields[key], answer
                ):
                    raise ValueError(
                        "Profile answer evidence changed; prepare answers again"
                    )
            elif derived:
                from .form_answers import FormAnswers

                if key not in fields or not FormAnswers(self.db).valid(
                    app, fields[key], answer
                ):
                    raise ValueError(
                        "Prepared answer evidence changed; prepare answers again"
                    )
            if not Memory(self.db)._valid(answer):
                raise ValueError(f"Answer evidence changed: {key}")
            facts = self.db.all(
                "SELECT * FROM facts WHERE id=ANY(%s)", (answer["fact_ids"],)
            )
            if not facts or any(
                f["candidate"] != app["candidate"]
                or (f["scope"] not in (app["id"], "global"))
                or (
                    f["scope"] == "global"
                    and not derived
                    and (
                        not fact_allows_global(f)
                        or not allows_global(key, fields.get(key))
                    )
                )
                for f in facts
            ):
                raise ValueError("Answer scope does not match this application")
            answers[key] = answer
        return {
            "application_id": app["id"],
            "employer": app["employer"],
            "role": app["role"],
            "requisition": app["requisition"],
            "application_url": app["official"].get("application_url"),
            "answers": answers,
            "attachments": attachments,
            "salary": answers.get("salary"),
            "consents": p.get("consents", {}),
            "form_schema": p.get("form_schema", []),
            "form_plan": p.get("form_plan"),
            "policy": POLICY,
        }

    def review(self, aid):
        app = self.get(aid)
        payload = self.review_payload(app)
        return {
            "state": app["state"],
            "payload": payload,
            "hash": digest(payload),
            "revision": app["revision"],
            "questions": self.db.all(
                "SELECT * FROM questions WHERE application_id=%s AND (answer_fact IS NULL OR needs_review)",
                (aid,),
            ),
        }

    def review_handoff(self, aid, reviewed_hash):
        """Record review of a manual package, without authorizing a submission."""
        from .forms import FieldSpec, valid_value

        with self.db.tx() as c:
            app = self.get(aid, c)
            self.editable(app)
            if app["state"] != "manual_handoff" or app["paused"]:
                raise ValueError("An active manual handoff is required")
            if c.execute(
                "SELECT 1 FROM attempts WHERE application_id=%s", (aid,)
            ).fetchone():
                raise ValueError(
                    "Reconcile the existing submission attempt before any further sending"
                )
            if c.execute(
                "SELECT 1 FROM questions WHERE application_id=%s AND (answer_fact IS NULL OR needs_review)",
                (aid,),
            ).fetchone():
                raise ValueError(
                    "Resolve the pending answers before reviewing the package"
                )
            payload = self.review_payload(app)
            if digest(payload) != reviewed_hash:
                raise ValueError(
                    "The package changed. Review the current answers and files"
                )
            if not payload["application_url"] or not app["payload"].get("form_plan"):
                raise ValueError(
                    "An inspected employer form and official route are required"
                )
            if not any(f["kind"] == "resume.pdf" for f in payload["attachments"]):
                raise ValueError("Select the resume PDF before reviewing the package")
            for field in payload["form_schema"]:
                if field.get("required") and field["type"] != "file":
                    answer = payload["answers"].get(field["key"])
                    if not answer or not valid_value(
                        FieldSpec(**field), answer.get("value")
                    ):
                        raise ValueError(
                            "Complete all required answers before reviewing the package"
                        )
            record = {"reviewed_hash": reviewed_hash, "kind": "manual_package_review"}
            c.execute(
                "UPDATE applications SET payload=%s,revision=revision+1,updated_at=now() WHERE id=%s",
                (js(app["payload"] | {"handoff_review": record}), aid),
            )
            self.db.audit("handoff.package_reviewed", aid, record, c)
        return record

    def update_payload(self, aid, patch):
        with self.db.tx() as c:
            app = self.get(aid, c)
            self.editable(app)
            if (
                set(patch) - {"browser_hash"}
                and c.execute(
                    "SELECT 1 FROM attempts WHERE application_id=%s", (aid,)
                ).fetchone()
            ):
                raise ValueError("Attempted application payloads are immutable")
            if app["state"] in (
                "submission_attempt",
                "receipt_verification",
                "submitted",
                "unresolved",
                "follow_up",
                "rejected",
            ):
                raise ValueError("Attempted or submitted payloads are immutable")
            if set(patch) - {
                "answers",
                "attachments",
                "consents",
                "form_schema",
                "form_plan",
                "browser_hash",
                "package",
                "scenario",
            }:
                raise ValueError("Unknown payload property")
            payload = app["payload"] | patch
            state = "review" if app["state"] == "authorized" else app["state"]
            c.execute(
                "UPDATE applications SET payload=%s,state=%s,revision=revision+1,updated_at=now() WHERE id=%s",
                (js(payload), state, aid),
            )
            c.execute(
                "UPDATE approvals SET invalidated=true WHERE application_id=%s", (aid,)
            )
            self.db.audit("payload.changed", aid, {"fields": sorted(patch)}, c)
        return self.get(aid)

    def ask(self, aid, key, prompt, resolution=None):
        app = self.get(aid)
        self.editable(app)
        qid = uid()
        resolution = resolution or {}
        if resolution.get("reason"):
            reason = resolution["reason"]
        elif resolution.get("conflicting_facts"):
            reason = {
                "code": "conflicting_evidence",
                "message": "Available records disagree. Confirm the current answer for this application.",
                "fact_ids": [f["id"] for f in resolution["conflicting_facts"]],
            }
        elif resolution.get("source") and resolution["source"] != "question":
            reason = {
                "code": "incompatible_answer",
                "message": "The saved answer does not match the employer's available choices.",
                "fact_ids": resolution.get("fact_ids", []),
            }
        else:
            reason = {
                "code": "confirmation_needed",
                "message": "No current confirmed answer is available for this item. Retrieved documents and generated drafts cannot supply your confirmation.",
            }
        self.db.execute(
            "INSERT INTO questions(id,application_id,key,prompt,scope,reason) VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(application_id,key) DO UPDATE SET prompt=EXCLUDED.prompt,reason=EXCLUDED.reason,needs_review=(questions.answer_fact IS NOT NULL)",
            (qid, aid, key, prompt, aid, js(reason)),
        )
        return self.db.one(
            "SELECT * FROM questions WHERE application_id=%s AND key=%s", (aid, key)
        )

    def require_answers(self, aid):
        """Save a manual answer handoff while retaining its resumable workflow state."""
        from .browser import BrowserWorker
        from .tracker import Tracker

        app = self.get(aid)
        if app["state"] != "missing_answers":
            self.transition(aid, "missing_answers")
        reason = "Manual answers needed: confirmed evidence is missing, conflicting, stale, or incompatible with the employer form."
        self.db.execute(
            "INSERT INTO followups(id,application_id,candidate,category,detail) VALUES(%s,%s,%s,'manual_application',%s) "
            "ON CONFLICT(id) DO UPDATE SET detail=EXCLUDED.detail,resolved=false",
            (
                digest([aid, "answer_handoff"]),
                aid,
                app["candidate"],
                js(
                    {
                        "reason": reason,
                        "kind": "answer_confirmation",
                        "receipt": "not_attempted",
                        "next_step": "Confirm the outstanding answers in the app. Preparation resumes after all answers are resolved; sending still needs approval.",
                    }
                ),
            ),
        )
        result = BrowserWorker(self.db).handoff(aid, reason)
        Tracker(self.db).manual_report(app["candidate"])
        return result

    def answer(self, question_id, value, scope="application", context=None):
        q = self.db.one("SELECT * FROM questions WHERE id=%s", (question_id,))
        if not q:
            raise ValueError("Question not found")
        app = self.get(q["application_id"])
        self.editable(app)
        if self.db.one("SELECT 1 FROM attempts WHERE application_id=%s", (app["id"],)):
            raise ValueError("Attempted application answers are immutable")
        if app["state"] in (
            "submission_attempt",
            "receipt_verification",
            "submitted",
            "unresolved",
            "follow_up",
            "rejected",
        ):
            raise ValueError("Attempted or submitted answers are immutable")
        for field in app["payload"].get("form_schema", []):
            if field.get("name") and field["key"] == q["key"]:
                from .forms import FieldSpec, valid_value

                if not valid_value(FieldSpec.model_validate(field), value):
                    raise ValueError(
                        "Choose an available employer option or provide a nonempty answer"
                    )
        field = next(
            (f for f in app["payload"].get("form_schema", []) if f["key"] == q["key"]),
            None,
        )
        from .form_policy import narrative_choice_mismatch, narrative_context_key

        if context is not None and (
            not isinstance(context, str)
            or len(context) > 12000
            or not field
            or not narrative_choice_mismatch(field)
        ):
            raise ValueError(
                "Narrative context is available only for a mismatched employer question, up to 12000 characters"
            )
        if scope not in ("application", "global"):
            raise ValueError("Unknown answer scope")
        if scope == "global" and not allows_global(q["key"], field):
            raise ValueError("This answer requires an application scope")
        actual_scope = app["id"] if scope == "application" else "global"
        old = (
            self.db.one("SELECT * FROM facts WHERE id=%s", (q["answer_fact"],))
            if q["answer_fact"]
            else None
        )
        fact = Memory(self.db).put(
            FactInput(
                candidate=app["candidate"],
                kind="answer",
                key=q["key"],
                value=value,
                status="confirmed",
                scope=actual_scope,
                provenance={"type": "user_confirmation", "question_id": question_id}
                | ({"form_field": field} if field else {})
                | ({"global_form_field": field} if scope == "global" and field else {}),
                supersedes=old["id"] if old and old["scope"] == actual_scope else None,
            ),
            resolve_scope=True,
            replace_scope=app["id"] if scope == "global" else None,
        )
        answer = Memory(self.db).resolve(q["key"], app["candidate"], app["id"])
        reliable = answer.get("source") != "question"
        self.db.execute(
            "UPDATE questions SET answer_fact=%s,needs_review=false WHERE id=%s",
            (fact["id"], question_id),
        )
        if not reliable:
            self.ask(app["id"], q["key"], q["prompt"], answer)
            # A user confirmation does not silently resolve other contradictory facts.
            # Keep this question open and do not insert an unresolved answer in a payload.
            return {"fact_id": fact["id"], "remaining": True, "needs_review": True}
        self.update_payload(
            app["id"],
            {"answers": app["payload"].get("answers", {}) | {q["key"]: answer}},
        )
        if context is not None:
            Memory(self.db).put(
                FactInput(
                    candidate=app["candidate"],
                    kind="answer",
                    key=narrative_context_key(field),
                    value=context.strip(),
                    status="confirmed",
                    scope=app["id"],
                    provenance={
                        "type": "user_confirmation",
                        "question_id": question_id,
                        "context_for_field": field["key"],
                        "destination": "application_notes",
                    },
                ),
                resolve_scope=True,
            )
        remaining = self.db.one(
            "SELECT 1 FROM questions WHERE application_id=%s AND (answer_fact IS NULL OR needs_review)",
            (app["id"],),
        )
        if not remaining and app["state"] == "missing_answers":
            from .queue import enqueue
            from .tracker import Tracker

            self.db.execute(
                "UPDATE followups SET resolved=true WHERE id=%s",
                (digest([app["id"], "answer_handoff"]),),
            )
            Tracker(self.db).manual_report(app["candidate"])
            return enqueue(self.db, "fill", {"application_id": app["id"]})
        return {"fact_id": fact["id"], "remaining": bool(remaining)}

    def authorize(self, aid, request: ApprovalInput):
        if (
            self.get(aid)["candidate"] != "fixture"
            and not self.db.settings.live_applications
        ):
            raise ValueError(
                "Real sending is disabled in this deployment; the prepared application remains available for review"
            )
        with self.db.tx() as c:
            app = self.get(aid, c)
            self.editable(app)
            from .progress import document_review_needed

            if document_review_needed(app):
                raise ValueError(
                    "Review the current document version before approving submission"
                )
            if app["payload"].get("form_plan"):
                from .forms import FormPreparation

                FormPreparation(self.db).validate_ready(app)
            elif app["candidate"] != "fixture":
                raise ValueError("A supported employer form plan is required")
            if app["state"] != "review":
                raise ValueError("Complete the form and review before approving")
            review = self.review(aid)
            if review["questions"]:
                raise ValueError("Required answers remain unresolved")
            if review["hash"] != request.reviewed_hash:
                raise ValueError("Review changed; inspect the updated payload")
            if app["payload"].get("browser_hash") != review["hash"]:
                raise ValueError("Visible form no longer matches the reviewed payload")
            if not review["payload"]["attachments"]:
                raise ValueError("Choose the documents actually attached")
            auth = uid()
            c.execute(
                "INSERT INTO approvals(id,application_id,payload_hash,actor) VALUES(%s,%s,%s,'user-ui')",
                (auth, aid, review["hash"]),
            )
            self.transition(
                aid,
                "authorized",
                {"approval_id": auth, "payload_hash": review["hash"]},
                c,
            )
        from .queue import enqueue

        return enqueue(self.db, "submit", {"application_id": aid}, max_attempts=3)

    def greenhouse_stopped_before_send(self, app, attempt):
        """Only a completed guarded run can establish that no POST was released."""
        if (
            app["payload"].get("form_plan", {}).get("spec", {}).get("adapter")
            != "greenhouse"
            or not attempt
            or attempt["result"].get("send_started")
        ):
            return False
        event = self.db.one(
            """SELECT detail FROM audit WHERE event='greenhouse.attempt_finished'
            AND subject=%s AND detail->>'attempt_id'=%s ORDER BY seq DESC LIMIT 1""",
            (app["id"], attempt["id"]),
        )
        return bool(
            event
            and event["detail"].get("post_observed") is False
            and event["detail"].get("post_status") is None
            and event["detail"].get("run_id") == attempt["result"].get("run_id")
        )

    def greenhouse_verification_stop(self, app, attempt):
        """A completed initial 428 run, never an unknown later continuation."""
        if (
            not attempt
            or app["payload"].get("form_plan", {}).get("spec", {}).get("adapter")
            != "greenhouse"
            or not attempt["result"].get("run_id")
            or not attempt["result"].get("send_started")
            or attempt["result"].get("verification_count", 0) != 0
        ):
            return None
        event = self.db.one(
            """SELECT seq,at,detail FROM audit WHERE event='greenhouse.attempt_finished'
            AND subject=%s AND detail->>'attempt_id'=%s ORDER BY seq DESC LIMIT 1""",
            (app["id"], attempt["id"]),
        )
        if (
            not event
            or event["detail"].get("run_id") != attempt["result"]["run_id"]
            or event["detail"].get("post_observed") is not True
            or event["detail"].get("post_status") != 428
            or event["detail"].get("receipt_status") is not None
            or self.db.one(
                """SELECT 1 FROM jobs WHERE input->>'application_id'=%s
                AND kind='continue_verification' AND created_at >= %s""",
                (app["id"], event["at"]),
            )
        ):
            return None
        return event

    def confirmed_verification_stop(self, app, attempt):
        event = self.greenhouse_verification_stop(app, attempt)
        confirmation = (
            attempt["result"].get("not_submitted_confirmation", {}) if attempt else {}
        )
        return bool(
            event
            and confirmation.get("run_id") == attempt["result"]["run_id"]
            and confirmation.get("payload_hash") == attempt["payload_hash"]
            and confirmation.get("stop_event") == event["seq"]
            and confirmation.get("actor") == "user-ui"
        )

    def submission_recovery(self, aid, validate=False):
        app = self.get(aid)
        attempt = self.db.one("SELECT * FROM attempts WHERE application_id=%s", (aid,))
        if (
            app["state"] != "unresolved"
            or app["imported"]
            or app["paused"]
            or self.db.one("SELECT 1 FROM receipts WHERE application_id=%s", (aid,))
            or self.db.one(
                "SELECT 1 FROM jobs WHERE input->>'application_id'=%s AND status IN ('queued','running')",
                (aid,),
            )
        ):
            return {"can_retry": False}
        stopped = self.greenhouse_stopped_before_send(app, attempt)
        verification = self.greenhouse_verification_stop(app, attempt)
        if not stopped and not verification:
            return {"can_retry": False}
        if app["candidate"] != "fixture" and not self.db.settings.live_applications:
            return {"can_retry": False, "reason": "Real sending is disabled."}
        if app["payload"].get("browser_hash") != attempt[
            "payload_hash"
        ] or not self.db.one(
            "SELECT 1 FROM approvals WHERE id=%s AND NOT invalidated AND payload_hash=%s",
            (attempt["approval_id"], attempt["payload_hash"]),
        ):
            return {
                "can_retry": False,
                "reason": "The original approval is no longer valid.",
            }
        try:
            if validate:
                from .forms import FormPreparation

                FormPreparation(self.db).validate_ready(app)
                review = self.review(aid)
                if review["questions"] or review["hash"] != attempt["payload_hash"]:
                    raise ValueError("The approved application changed.")
        except ValueError as exc:
            return {"can_retry": False, "reason": str(exc)}
        if verification and not self.confirmed_verification_stop(app, attempt):
            return {
                "can_retry": False,
                "can_confirm_not_submitted": True,
                "kind": "verification",
                "attempt_id": attempt["id"],
                "run_id": attempt["result"]["run_id"],
                "reviewed_hash": attempt["payload_hash"],
                "reason": "Greenhouse requested verification and no receipt is recorded. Confirm whether verification and submission remained incomplete.",
            }
        return {
            "can_retry": True,
            "kind": "verification" if verification else "before_send",
            "reason": (
                "You confirmed that verification and submission were not completed. Retry will open a new browser using the existing approval and files."
                if verification
                else "The browser stopped before sending the final application request. Retry will recheck your existing approval and files."
            ),
        }

    def confirm_not_submitted(self, aid, request: NotSubmittedInput):
        with self.db.tx() as c:
            app = self.get(aid, c)
            self.editable(app)
            recovery = self.submission_recovery(aid)
            if not recovery.get("can_confirm_not_submitted") or any(
                getattr(request, key) != recovery.get(key)
                for key in ("attempt_id", "run_id", "reviewed_hash")
            ):
                raise ValueError(
                    "The attempt changed or has no completed verification stop. Check the current outcome."
                )
            attempt = self.db.one(
                "SELECT * FROM attempts WHERE application_id=%s", (aid,)
            )
            event = self.greenhouse_verification_stop(app, attempt)
            confirmation = {
                "actor": "user-ui",
                "run_id": request.run_id,
                "payload_hash": request.reviewed_hash,
                "stop_event": event["seq"],
                "statement": "Verification was not completed and the application was not submitted in another browser.",
            }
            c.execute(
                "UPDATE attempts SET result=result || %s WHERE id=%s",
                (js({"not_submitted_confirmation": confirmation}), attempt["id"]),
            )
            self.db.audit(
                "submission.not_submitted_confirmed",
                aid,
                {
                    "attempt_id": attempt["id"],
                    **confirmation,
                },
                c,
            )
        return {"state": app["state"], "can_retry": True, "sending_queued": False}

    def retry_submission(self, aid):
        with self.db.tx() as c:
            app = self.get(aid, c)
            self.editable(app)
            recovery = self.submission_recovery(aid, validate=True)
            if not recovery["can_retry"]:
                raise ValueError(
                    recovery.get(
                        "reason",
                        "Reconcile the existing submission outcome before retrying.",
                    )
                )
            c.execute(
                "UPDATE attempts SET state='retry_ready' WHERE application_id=%s",
                (aid,),
            )
            # Dedicated recovery gate, not an unrestricted state transition.
            c.execute(
                "UPDATE applications SET state='authorized',revision=revision+1,updated_at=now() WHERE id=%s",
                (aid,),
            )
            c.execute(
                "UPDATE followups SET resolved=true WHERE id=%s",
                (digest([aid, "manual_application"]),),
            )
            self.db.audit(
                "submission.retry_requested", aid, {"reuses_approval": True}, c
            )
        from .queue import enqueue

        return enqueue(self.db, "submit", {"application_id": aid}, max_attempts=1)

    def begin_attempt(self, aid, mark_attached=True):
        with self.db.tx() as c:
            app = self.get(aid, c)
            self.editable(app)
            if app["state"] != "authorized":
                raise ValueError("Matching application authorization is required")
            payload = self.review_payload(app)
            hash_ = digest(payload)
            approval = c.execute(
                "SELECT * FROM approvals WHERE application_id=%s AND payload_hash=%s AND NOT invalidated ORDER BY created_at DESC LIMIT 1",
                (aid, hash_),
            ).fetchone()
            if not approval:
                raise ValueError("Authorization is absent or no longer matches")
            existing = c.execute(
                "SELECT * FROM attempts WHERE application_id=%s", (aid,)
            ).fetchone()
            if existing and (
                mark_attached
                or existing["state"] != "retry_ready"
                or existing["payload_hash"] != hash_
                or existing["approval_id"] != approval["id"]
                or not (
                    self.greenhouse_stopped_before_send(app, existing)
                    or self.confirmed_verification_stop(app, existing)
                )
            ):
                raise ValueError(
                    "An attempt already exists; reconcile it before any retry"
                )
            attempt = existing["id"] if existing else uid()
            if existing:
                self.db.audit(
                    "submission.previous_run_preserved",
                    aid,
                    {
                        "attempt_id": existing["id"],
                        "result": existing["result"],
                    },
                    c,
                )
                c.execute(
                    "UPDATE attempts SET state='started',result=%s WHERE id=%s",
                    (js({"run_id": uid(), "send_started": False}), attempt),
                )
            else:
                c.execute(
                    "INSERT INTO attempts(id,application_id,payload_hash,approval_id,state,result) VALUES(%s,%s,%s,%s,'started',%s)",
                    (
                        attempt,
                        aid,
                        hash_,
                        approval["id"],
                        js({"run_id": uid(), "send_started": False}),
                    ),
                )
            self.transition(aid, "submission_attempt", {"attempt_id": attempt}, c)
            for artifact in payload["attachments"]:
                c.execute(
                    "UPDATE artifacts SET attached=attached OR %s,immutable=true WHERE id=%s",
                    (mark_attached, artifact["id"]),
                )
            # Durable attempt exists before the browser can perform a final action.
        frozen = self.db.settings.state / "submitted-payloads" / f"{attempt}.json"
        atomic_write(frozen, json.dumps(payload, indent=2, default=str))
        for artifact in payload["attachments"]:
            row = self.db.one("SELECT * FROM artifacts WHERE id=%s", (artifact["id"],))
            source = Path(row["path"])
            target = (
                self.db.settings.state
                / "submitted-documents"
                / artifact["hash"]
                / source.name
            )
            if not target.exists():
                atomic_write(target, source.read_bytes())
                target.chmod(0o400)
        return attempt

    def record_receipt(self, aid, attempt_id, evidence, kind="employer_confirmation"):
        if kind not in ("employer_confirmation", "user_confirmation"):
            raise ValueError("Unsupported receipt type")
        if (
            not evidence.get("confirmed")
            or not evidence.get("text")
            or evidence.get("requisition") != self.get(aid)["requisition"]
        ):
            raise ValueError("Receipt must identify the requisition and confirmation")
        with self.db.tx() as c:
            app = self.get(aid, c)
            self.editable(app)
            if app["state"] == "submitted":
                return {"state": "submitted", "already_recorded": True}
            if app["state"] not in (
                "submission_attempt",
                "receipt_verification",
                "unresolved",
                "manual_handoff",
                "awaiting_challenge",
            ):
                raise ValueError("No submission outcome to reconcile")
            if (
                attempt_id
                and not c.execute(
                    "SELECT 1 FROM attempts WHERE id=%s AND application_id=%s",
                    (attempt_id, aid),
                ).fetchone()
            ):
                raise ValueError("Receipt attempt mismatch")
            rid = uid()
            c.execute(
                "INSERT INTO receipts(id,application_id,attempt_id,kind,evidence) VALUES(%s,%s,%s,%s,%s)",
                (rid, aid, attempt_id, kind, js(evidence)),
            )
            c.execute(
                "UPDATE applications SET state='submitted',revision=revision+1,updated_at=now() WHERE id=%s",
                (aid,),
            )
            c.execute(
                "UPDATE attempts SET state='confirmed',result=result || %s WHERE id=%s",
                (js({"receipt_id": rid}), attempt_id),
            )
            c.execute(
                "UPDATE followups SET resolved=true WHERE application_id=%s", (aid,)
            )
            self.db.audit(
                "receipt.confirmed", aid, {"receipt_id": rid, "kind": kind}, c
            )
        from .tracker import Tracker

        Tracker(self.db).manual_report(app["candidate"])
        return {"state": "submitted", "receipt_id": rid}

    def unresolved(self, aid, reason):
        with self.db.tx() as c:
            app = self.get(aid, c)
            self.editable(app)
            if app["state"] == "submitted":
                return
            c.execute(
                "UPDATE applications SET state='unresolved',revision=revision+1 WHERE id=%s",
                (aid,),
            )
            c.execute(
                "UPDATE attempts SET state='unresolved',result=result || %s WHERE application_id=%s AND state<>'confirmed'",
                (js({"reason": reason}), aid),
            )
            fid = digest([aid, "manual_application"])
            c.execute(
                "INSERT INTO followups(id,application_id,candidate,category,detail) VALUES(%s,%s,%s,'manual_application',%s) ON CONFLICT(id) DO UPDATE SET detail=EXCLUDED.detail,resolved=false",
                (
                    fid,
                    aid,
                    app["candidate"],
                    js(
                        {
                            "reason": reason,
                            "receipt": "absent",
                            "next_step": "Reconcile employer confirmation before retrying",
                        }
                    ),
                ),
            )
            self.db.audit("submission.unresolved", aid, {"reason": reason}, c)

        from .tracker import Tracker

        Tracker(self.db).manual_report(app["candidate"])

    def discard(self, aid, reason="Discarded by the user"):
        from .attention_summary import discard_blocker

        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("Record a reason of at most 2000 characters")
        with self.db.tx() as c:
            app = self.get(aid, c)
            jobs = c.execute(
                "SELECT id,status FROM jobs WHERE input->>'application_id'=%s AND status IN ('queued','running') FOR UPDATE",
                (aid,),
            ).fetchall()
            attempted = c.execute(
                "SELECT 1 FROM attempts WHERE application_id=%s", (aid,)
            ).fetchone()
            received = c.execute(
                "SELECT 1 FROM receipts WHERE application_id=%s", (aid,)
            ).fetchone()
            blocked = discard_blocker(
                app,
                bool(attempted),
                bool(received),
                any(j["status"] == "running" for j in jobs),
            )
            if blocked:
                raise ValueError(blocked)
            c.execute(
                "UPDATE applications SET state='skipped',paused=false,revision=revision+1,updated_at=now() WHERE id=%s",
                (aid,),
            )
            c.execute(
                "UPDATE approvals SET invalidated=true WHERE application_id=%s", (aid,)
            )
            c.execute(
                "UPDATE jobs SET status='cancelled',cancel_requested=true,updated_at=now() WHERE id=ANY(%s)",
                ([j["id"] for j in jobs],),
            )
            c.execute(
                "UPDATE followups SET resolved=true WHERE application_id=%s", (aid,)
            )
            c.execute(
                "UPDATE notifications SET resolved_at=now(),updated_at=now() WHERE application_id=%s AND resolved_at IS NULL",
                (aid,),
            )
            c.execute(
                "UPDATE hunt_leads SET status='discarded',reason=%s,updated_at=now() WHERE application_id=%s",
                (reason, aid),
            )
            self.db.audit(
                "workflow.discarded",
                aid,
                {"reason": reason, "prior_state": app["state"]},
                c,
            )
        from .tracker import Tracker

        Tracker(self.db).manual_report(app["candidate"])
        return self.get(aid)

    def control(self, aid, action, reason=""):
        if action == "discard":
            return self.discard(aid, reason or "Discarded by the user")
        with self.db.tx() as c:
            app = self.get(aid, c)
            self.editable(app)
            if action in ("pause", "resume"):
                c.execute(
                    "UPDATE applications SET paused=%s WHERE id=%s",
                    (action == "pause", aid),
                )
                self.db.audit("workflow." + action, aid, {}, c)
            elif action in ("defer", "skip", "close"):
                if not reason:
                    raise ValueError("Record a reason for this decision")
                target = {"defer": "deferred", "skip": "skipped", "close": "closed"}[
                    action
                ]
                self.transition(aid, target, {"reason": reason}, c)
                c.execute(
                    "UPDATE approvals SET invalidated=true WHERE application_id=%s",
                    (aid,),
                )
            else:
                raise ValueError("Unknown workflow action")
        if action in ("defer", "skip", "close"):
            from .tracker import Tracker

            Tracker(self.db).manual_report(app["candidate"])
        return self.get(aid)
