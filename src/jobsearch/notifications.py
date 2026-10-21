"""A durable inbox projected from workflow blockers, never a source of authority."""

from .config import digest
from .db import js
from .requirement_decisions import requirement_key
from .attention_summary import initial_scores, concerns, discard_blocker


class Notifications:
    def __init__(self, db):
        self.db = db

    def snapshot(self, candidate, include_resolved=False):
        if candidate not in ("real", "fixture"):
            raise ValueError("Unknown candidate")
        with self.db.tx() as c:
            # Serialize projection and acknowledgment. Repeated polls do not create
            # duplicate alerts or turn a seen, unchanged alert into an unread one.
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", ("inbox:" + candidate,)
            )
            apps = c.execute(
                "SELECT * FROM applications WHERE candidate=%s", (candidate,)
            ).fetchall()
            questions = c.execute(
                "SELECT q.* FROM questions q JOIN applications a ON a.id=q.application_id "
                "WHERE a.candidate=%s AND (q.answer_fact IS NULL OR q.needs_review) ORDER BY q.id",
                (candidate,),
            ).fetchall()
            jobs = c.execute(
                "SELECT DISTINCT ON (j.input->>'application_id',j.kind) j.* FROM jobs j "
                "JOIN applications a ON a.id=j.input->>'application_id' WHERE a.candidate=%s "
                "ORDER BY j.input->>'application_id',j.kind,j.created_at DESC,j.id DESC",
                (candidate,),
            ).fetchall()
            followups = c.execute(
                "SELECT id,application_id,detail FROM followups WHERE candidate=%s AND NOT resolved AND category='manual_application' ORDER BY id",
                (candidate,),
            ).fetchall()
            originals = initial_scores(c, candidate)
            attempted = {
                r["application_id"]
                for r in c.execute(
                    "SELECT t.application_id FROM attempts t JOIN applications a ON a.id=t.application_id WHERE a.candidate=%s UNION SELECT r.application_id FROM receipts r JOIN applications a ON a.id=r.application_id WHERE a.candidate=%s",
                    (candidate, candidate),
                ).fetchall()
            }
            running = {
                r["application_id"]
                for r in c.execute(
                    "SELECT j.input->>'application_id' AS application_id FROM jobs j JOIN applications a ON a.id=j.input->>'application_id' WHERE a.candidate=%s AND j.status='running'",
                    (candidate,),
                ).fetchall()
            }
            summaries = {}
            for app in apps:
                app["has_attempt"] = app["id"] in attempted
                ranked = concerns(app["score"])
                blocked = discard_blocker(
                    app, app["id"] in attempted, running=app["id"] in running
                )
                summaries[app["id"]] = {
                    "initial_score": originals.get(app["id"]),
                    "current_score": app["score"].get("total"),
                    "concerns": ranked[:3],
                    "additional_concerns": max(0, len(ranked) - 3),
                    "can_discard": blocked is None,
                    "discard_blocker": blocked,
                }
            active = []
            for app in apps:
                if app["state"] == "unresolved" and not app["imported"]:
                    from .workflow import Workflow

                    app["submission_recovery"] = Workflow(self.db).submission_recovery(
                        app["id"]
                    )
                for kind, detail in self.issues(app, questions, jobs, followups):
                    nid = digest([app["id"], kind])
                    active.append(nid)
                    c.execute(
                        "INSERT INTO notifications(id,application_id,kind,detail,fingerprint) VALUES(%s,%s,%s,%s,%s) "
                        "ON CONFLICT(application_id,kind) DO UPDATE SET detail=EXCLUDED.detail,fingerprint=EXCLUDED.fingerprint, "
                        "read_at=NULL,resolved_at=NULL,updated_at=now() "
                        "WHERE notifications.fingerprint<>EXCLUDED.fingerprint OR notifications.resolved_at IS NOT NULL",
                        (nid, app["id"], kind, js(detail), digest(detail)),
                    )
            c.execute(
                "UPDATE notifications n SET resolved_at=now(),updated_at=now() FROM applications a "
                "WHERE n.application_id=a.id AND a.candidate=%s AND n.resolved_at IS NULL AND NOT (n.id=ANY(%s))",
                (candidate, active),
            )
            rows = c.execute(
                "SELECT n.*,a.employer,a.role,a.requisition,a.state,a.imported,a.paused FROM notifications n "
                "JOIN applications a ON a.id=n.application_id WHERE a.candidate=%s "
                "AND (%s OR n.resolved_at IS NULL) ORDER BY (n.resolved_at IS NULL) DESC,n.updated_at DESC,n.id",
                (candidate, include_resolved),
            ).fetchall()
        rows = [row | summaries[row["application_id"]] for row in rows]
        pending = [n for n in rows if not n["resolved_at"]]
        return {
            "items": rows,
            "pending": len(pending),
            "applications": len({n["application_id"] for n in pending}),
            "unread": sum(n["read_at"] is None for n in pending),
        }

    @staticmethod
    def issues(app, questions, jobs, followups):
        state, aid = app["state"], app["id"]
        if state in (
            "submitted",
            "follow_up",
            "rejected",
            "closed",
            "skipped",
            "deferred",
        ):
            return []
        pending = [q for q in questions if q["application_id"] == aid]
        manual = [f for f in followups if f["application_id"] == aid]
        detail = None
        if state == "unresolved":
            recovery = app.get("submission_recovery", {})
            detail = (
                (
                    "retry",
                    "Approved application stopped before sending",
                    "The guarded browser recorded no final application request. Open the application to retry using its existing approval.",
                )
                if recovery.get("can_retry")
                else (
                    "receipt",
                    "Receipt needs checking",
                    "Check the employer confirmation before any retry. A click is not proof of receipt.",
                )
            )
            if recovery.get("can_confirm_not_submitted"):
                detail = (
                    "verification_outcome",
                    "Resolve the unfinished verification",
                    "Record whether verification and submission were completed. A verification-code email is not an application receipt.",
                )
            elif recovery.get("can_retry") and recovery.get("kind") == "verification":
                detail = (
                    "retry",
                    "Approved application is ready to retry",
                    "You confirmed that submission was not completed. Open the application to retry using its existing approval.",
                )
        elif state == "awaiting_challenge":
            detail = (
                "challenge",
                "Browser verification needs you",
                "Complete the verification in your browser. Do not enter verification codes in career answers.",
            )
        elif pending and state not in ("submission_attempt", "receipt_verification"):
            detail = (
                "answers",
                "Manual answers needed",
                "The agent cannot reliably answer these items from confirmed evidence. Answer or correct them before continuing.",
            )
        elif state == "manual_handoff":
            spec = (app["payload"].get("form_plan") or {}).get("spec", {})
            refreshable = {
                "Greenhouse browser completion needs a portal adapter; use the saved answer plan",
                "Additional demographic or consent questions need review before browser filling",
            }
            unsupported = set(spec.get("unsupported", []))
            if app.get("has_attempt"):
                detail = (
                    "receipt",
                    "Check the previous submission outcome",
                    "Reconcile employer confirmation before trying another submission.",
                )
            elif (
                not app["imported"]
                and spec.get("adapter") == "greenhouse"
                and unsupported
                and unsupported <= refreshable
            ):
                detail = (
                    "handoff",
                    "Greenhouse browser filling is available",
                    "Open this application and retry browser filling. The agent will refresh the form and reuse your confirmed answers.",
                )
            elif (
                not app["imported"]
                and spec.get("adapter") == "greenhouse"
                and not unsupported
                and app["score"].get("scores")
                and not app["score"].get("primary")
            ):
                detail = (
                    "requirements",
                    "Review fit before sending",
                    " ".join(app["score"].get("reasons", []))
                    or "Review the fit assessment before continuing to submission.",
                )
            else:
                detail = (
                    "handoff",
                    "Complete this application manually",
                    "Open the saved handoff and official application route. Confirm the outcome here after checking the employer receipt.",
                )
        elif manual and state not in ("submission_attempt", "receipt_verification"):
            detail = (
                "followup",
                "Manual application follow-up needed",
                "This application has an outstanding manual follow-up. Inspect the recorded reason and next step.",
            )
        elif (
            state == "scored"
            and not app["imported"]
            and any(
                r.get("central")
                and r.get("classification") == "required"
                and r.get("material", True)
                and r.get("assessment")
                in ("partial", "unconfirmed", "gap", "hard_stop")
                and not any(
                    d["requirement_key"] == requirement_key(r)
                    for d in app["score"].get("requirement_decisions", [])
                )
                for r in app["score"].get("requirements", [])
            )
        ):
            detail = (
                "requirements",
                "Central requirements need your decision",
                "Confirm or deny the unresolved requirements in the fit assessment. Add a note if useful, then rescore the job.",
            )
        elif state == "prepared" and app["payload"].get("package", {}).get(
            "version"
        ) != app["payload"].get("document_review", {}).get("version"):
            detail = (
                "documents",
                "Application documents need your review",
                "Read the resume and cover letter. Suggest changes or mark the documents reviewed before continuing.",
            )
        elif state == "review":
            detail = (
                "approval",
                "Application ready for review",
                "Review the answers, attachments, and commitments. Sending requires your approval for this application.",
            )
        elif app["paused"]:
            detail = (
                "paused",
                "Application paused",
                "Resume this application when you are ready to continue.",
            )
        # Use the same current gate as the application page. Historical handoffs
        # must not direct the user away from a recoverable preparation step.
        from .progress import progress

        guide = progress(
            app
            | {
                "questions": pending,
                "jobs": [j for j in jobs if j["input"].get("application_id") == aid],
                "attempts": [True] if app.get("has_attempt") else [],
            },
            app["payload"].get("browser_hash"),
        )
        if guide and state not in ("discovered", "verified"):
            if guide["status"] == "working":
                detail = None
            else:
                kind = (
                    "paused"
                    if guide["status"] == "paused"
                    else (
                        (
                            "requirements"
                            if guide["status"] == "blocked"
                            or detail
                            and detail[0] == "requirements"
                            else "selection"
                        )
                        if guide["step"] == 0
                        else "documents"
                        if guide["step"] == 1
                        else "answers"
                        if pending
                        else "approval"
                        if guide["step"] == 3
                        else "continue"
                    )
                )
                detail = kind, guide["title"], guide["instruction"]
        issues = []
        if detail:
            kind, title, message = detail
            value = {"title": title, "message": message, "blocking": kind != "approval"}
            if kind == "answers":
                value["questions"] = [
                    {"id": q["id"], "prompt": q["prompt"], "reason": q["reason"]}
                    for q in pending
                ]
            if manual:
                value["followups"] = [
                    {
                        "id": f["id"],
                        "reason": f["detail"].get("reason"),
                        "next_step": f["detail"].get("next_step"),
                    }
                    for f in manual
                ]
            issues.append((kind, value))
        if not app["imported"] and state not in (
            "unresolved",
            "submission_attempt",
            "receipt_verification",
        ):
            failed = [
                j
                for j in jobs
                if j["input"].get("application_id") == aid and j["status"] == "failed"
            ]
            if failed:
                issues.append(
                    (
                        "worker",
                        {
                            "title": "Application work stopped",
                            "message": "A task exhausted its retries. Inspect the task result before resuming it.",
                            "blocking": True,
                            "jobs": [
                                {"id": j["id"], "kind": j["kind"]} for j in failed
                            ],
                        },
                    )
                )
        return issues

    def mark_read(self, nid, candidate):
        self.snapshot(candidate)
        with self.db.tx() as c:
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", ("inbox:" + candidate,)
            )
            row = c.execute(
                "UPDATE notifications n SET read_at=COALESCE(read_at,now()) FROM applications a "
                "WHERE n.id=%s AND a.id=n.application_id AND a.candidate=%s RETURNING n.id",
                (nid, candidate),
            ).fetchone()
            if not row:
                raise ValueError("Notification not found")
        return {"read": True}
