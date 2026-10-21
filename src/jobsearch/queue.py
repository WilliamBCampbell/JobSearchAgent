from __future__ import annotations
import asyncio
import signal
import traceback
from .db import uid, js


def enqueue(db, kind, payload, max_attempts=3):
    jid = uid()
    with db.tx() as c:
        if payload.get("application_id"):
            app = c.execute(
                "SELECT state FROM applications WHERE id=%s FOR UPDATE",
                (payload["application_id"],),
            ).fetchone()
            if app and app["state"] == "skipped":
                raise ValueError("Discarded roles cannot queue more work")
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (payload["application_id"] + kind,),
            )
            existing = c.execute(
                "SELECT id,status FROM jobs WHERE kind=%s AND input->>'application_id'=%s AND status IN ('queued','running') AND (kind NOT IN ('llm','draft_answer_context') OR input=%s)",
                (kind, payload["application_id"], js(payload)),
            ).fetchone()
            if existing:
                return {"job_id": existing["id"], "status": existing["status"]}
        c.execute(
            "INSERT INTO jobs(id,kind,input,max_attempts) VALUES(%s,%s,%s,%s)",
            (jid, kind, js(payload), max_attempts),
        )
    db.audit("job.queued", jid, {"kind": kind})
    return {"job_id": jid, "status": "queued"}


class Worker:
    def __init__(self, db):
        self.db = db
        self.id = uid()
        self.stop = False
        from .browser import BrowserWorker

        self.browser = BrowserWorker(db)

    def recover(self):
        from .workflow import Workflow

        expired = self.db.all(
            "SELECT * FROM jobs WHERE status='running' AND lease_until<now()"
        )
        for job in expired:
            self.db.execute(
                "UPDATE llm_runs SET status='interrupted',error='Worker stopped; a resumed evaluation uses a fresh process',pid=NULL,updated_at=now() WHERE job_id=%s AND status='running'",
                (job["id"],),
            )
            if job["kind"] in ("submit", "continue_verification"):
                aid = job["input"]["application_id"]
                if self.db.one(
                    "SELECT 1 FROM attempts WHERE application_id=%s", (aid,)
                ):
                    Workflow(self.db).unresolved(
                        aid,
                        "Worker lease expired after a submission attempt; reconcile before retrying",
                    )
                    self.db.execute(
                        "UPDATE jobs SET status='unresolved',error='Reconciliation required' WHERE id=%s",
                        (job["id"],),
                    )
                    continue
            self.db.execute(
                "UPDATE jobs SET status=CASE WHEN cancel_requested THEN 'cancelled' WHEN attempts>=max_attempts THEN 'failed' ELSE 'queued' END,worker=NULL WHERE id=%s",
                (job["id"],),
            )
        # Also reconcile attempts whose worker disappeared between the attempt and job completion.
        for a in self.db.all(
            "SELECT a.id FROM applications a WHERE a.state IN ('submission_attempt','receipt_verification') AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.input->>'application_id'=a.id AND j.status='running' AND j.lease_until>now())"
        ):
            Workflow(self.db).unresolved(
                a["id"], "Interrupted submission requires receipt reconciliation"
            )

    def claim(self):
        with self.db.tx() as c:
            c.execute("SELECT pg_advisory_xact_lock(917319)")
            row = c.execute(
                "SELECT j.* FROM jobs j WHERE j.status='queued' AND j.available_at<=now() AND NOT j.cancel_requested AND NOT EXISTS(SELECT 1 FROM applications a WHERE a.id=j.input->>'application_id' AND a.paused) AND NOT (j.kind IN ('llm','package','form_fit','rescore','fill') AND EXISTS(SELECT 1 FROM jobs busy WHERE busy.status='running' AND busy.kind IN ('llm','package','form_fit','rescore','fill'))) AND NOT EXISTS(SELECT 1 FROM hunts h WHERE h.status IN ('paused','cancelled') AND (h.id=j.input->>'hunt_id' OR h.checkpoint->>'search_id'=j.id OR EXISTS(SELECT 1 FROM hunt_leads hl WHERE hl.hunt_id=h.id AND hl.child_job=j.id))) AND NOT EXISTS(SELECT 1 FROM jobs r WHERE r.status='running' AND r.input->>'application_id'=j.input->>'application_id') ORDER BY (j.kind='hunt'), j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 1"
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE jobs SET status='running',attempts=attempts+1,worker=%s,lease_until=now()+interval '30 seconds',updated_at=now() WHERE id=%s",
                    (self.id, row["id"]),
                )
                row["attempts"] += 1
            return row

    def checkpoint(self, jid, value):
        self.db.execute(
            "UPDATE jobs SET checkpoint=%s,updated_at=now() WHERE id=%s AND worker=%s",
            (js(value), jid, self.id),
        )

    def cancelled(self, jid):
        row = self.db.one("SELECT cancel_requested FROM jobs WHERE id=%s", (jid,))
        return self.stop or not row or row["cancel_requested"]

    async def heartbeat(self, jid):
        while True:
            await asyncio.sleep(5)
            await asyncio.to_thread(
                self.db.execute,
                "UPDATE jobs SET lease_until=now()+interval '30 seconds' WHERE id=%s AND worker=%s AND status='running'",
                (jid, self.id),
            )

    async def dispatch(self, job):
        kind = job["kind"]
        data = job["input"]
        jid = job["id"]
        from .workflow import Workflow

        flow = Workflow(self.db)
        if kind == "draft_answer_context":
            from .form_answers import FormAnswers

            return await asyncio.to_thread(
                FormAnswers(self.db).draft_context,
                data["application_id"],
                data["question_id"],
                jid,
                lambda p: self.checkpoint(jid, p),
                lambda: self.cancelled(jid),
            )
        if kind == "refresh_opening":
            from .progress import ACTIVE
            from .openings import OfficialOpenings
            from .verification import Verification
            from .scoring import Assessment

            app = flow.get(data["application_id"])
            flow.editable(app)
            if (
                app["paused"]
                or app["state"] not in ACTIVE
                or self.db.one(
                    "SELECT 1 FROM attempts WHERE application_id=%s", (app["id"],)
                )
            ):
                raise ValueError("Opening checks require active work before submission")
            if app["candidate"] == "fixture":
                evidence = await asyncio.to_thread(
                    Verification(self.db).verify,
                    app,
                    self.db.settings.origin + "/fixture/posting/" + app["id"],
                    "",
                )
            else:
                url = app["official"].get("url") or app["official"].get(
                    "application_url"
                )
                if not url:
                    raise ValueError(
                        "An official employer URL is needed before verification"
                    )
                evidence = await asyncio.to_thread(
                    OfficialOpenings(self.db).verify,
                    {
                        "company": app["employer"],
                        "title": app["role"],
                        "requisition": app["requisition"],
                        "job_url": url,
                    },
                )
            if self.cancelled(jid):
                return {"cancelled": True}
            evidence = {
                "url": app["official"].get("url")
                or app["official"].get("application_url"),
                "application_url": app["official"].get("application_url"),
                "requisition": app["requisition"],
            } | evidence
            if (
                evidence.get("open")
                and str(evidence.get("requisition")) != app["requisition"]
            ):
                raise ValueError(
                    "The refreshed opening refers to a different requisition"
                )
            flow.set_official(app["id"], evidence)
            changed = bool(app["score"].get("evidence_changed")) or evidence.get(
                "source_hash"
            ) != app["official"].get("source_hash")
            if app["score"].get("scores") and flow.get(app["id"])["state"] != "closed":
                if changed:
                    self.db.execute(
                        'UPDATE applications SET score=score || \'{"evidence_changed":true,"primary":false}\'::jsonb WHERE id=%s',
                        (app["id"],),
                    )
                else:
                    flow.score(app["id"], Assessment.model_validate(app["score"]))
            return {
                "state": flow.get(app["id"])["state"],
                "opening_status": evidence.get("status"),
                "needs_rescore": changed and bool(app["score"]),
            }
        if kind == "hunt":
            from .hunt import Hunt

            return await Hunt(self.db).tick(job)
        if kind == "llm":
            from .llm.service import InferenceService
            from .llm.contracts import InferenceRequest

            return await asyncio.to_thread(
                InferenceService(self.db).run,
                InferenceRequest(**data),
                jid,
                lambda value: self.checkpoint(jid, value),
                lambda: self.cancelled(jid),
            )
        if kind == "rescore":
            from .llm.service import InferenceService
            from .llm.contracts import InferenceRequest

            app = flow.get(data["application_id"])
            flow.editable(app)
            if app["paused"] or app["state"] not in (
                "verified",
                "scored",
                "selected",
                "prepared",
                "missing_answers",
                "review",
                "manual_handoff",
            ):
                raise ValueError("The application is no longer ready for rescoring")
            if self.db.one(
                "SELECT 1 FROM attempts WHERE application_id=%s", (app["id"],)
            ):
                raise ValueError("Attempted applications cannot change fit")
            if self.cancelled(jid):
                return {"cancelled": True}
            service = InferenceService(self.db)
            result = await asyncio.to_thread(
                service.run,
                InferenceRequest(
                    task="fit_assessment",
                    candidate=app["candidate"],
                    application_id=app["id"],
                    question="Rescore this job using current evidence and the candidate's application-scoped requirement decisions and notes. Confirm means the candidate meets that exact requirement; Deny means they do not. Preserve all central requirements and every rubric category. Do not generalize these decisions to other claims or applications.",
                ),
                jid,
                lambda value: self.checkpoint(jid, value),
                lambda: self.cancelled(jid),
            )
            if self.cancelled(jid):
                return {"cancelled": True}
            updated = await asyncio.to_thread(service.apply_score, result["run_id"])
            return {
                "application_id": app["id"],
                "run_id": result["run_id"],
                "score": updated["score"],
                "state": updated["state"],
            }
        if kind == "search":
            from .search import SearchService

            return await asyncio.to_thread(
                SearchService(self.db).run,
                job,
                lambda v: self.checkpoint(jid, v),
                lambda: self.cancelled(jid),
            )
        if kind == "ingest":
            from .corpus import Corpus

            return await asyncio.to_thread(
                Corpus(self.db).sync,
                data.get("candidate", "real"),
                job["checkpoint"],
                lambda: self.cancelled(jid),
                lambda v: self.checkpoint(jid, v),
            )
        if kind == "import":
            from .intake import Intake

            return await asyncio.to_thread(
                Intake(self.db).run, data.get("candidate", "real")
            )
        if kind == "package":
            from .packages import Packages

            app = flow.get(data["application_id"])
            if not data.get("user_revision") and app["payload"].get("package", {}).get(
                "version"
            ):
                # A repeated click/restart cannot silently replace a reviewed version.
                if app["state"] == "preparing":
                    flow.transition(app["id"], "prepared")
                return app["payload"]["package"] | {"reused": True}
            if (
                data.get("user_revision")
                and app["payload"].get("package", {}).get("revision_job_id") == jid
            ):
                if app["state"] == "preparing":
                    flow.transition(app["id"], "prepared")
                return app["payload"]["package"]
            if (
                data.get("user_revision")
                and app["payload"].get("package", {}).get("version")
                != data["user_revision"]["base_version"]
            ):
                raise ValueError(
                    "The source package changed; request a revision of the current version"
                )
            if app["state"] in ("selected", "prepared", "review", "authorized"):
                flow.transition(app["id"], "preparing")
            elif app["state"] != "preparing":
                raise ValueError("Select the application before preparing a package")
            result = await asyncio.to_thread(
                Packages(self.db).prepare,
                app,
                data.get("include_cover", True),
                jid,
                lambda value: self.checkpoint(jid, value),
                lambda: self.cancelled(jid),
                data.get("user_revision"),
            )
            if self.cancelled(jid):
                return {"cancelled": True, "package": result}
            if data.get("user_revision"):
                result["revision_job_id"] = jid
            flow.update_payload(
                app["id"],
                {
                    "package": result,
                    "attachments": [
                        f["id"]
                        for f in result["files"]
                        if f["kind"]
                        in data.get("user_revision", {}).get(
                            "attachment_kinds", ["resume.pdf"]
                        )
                    ],
                },
            )
            flow.transition(app["id"], "prepared")
            return result
        if kind == "form_fit":
            from .llm.service import InferenceService
            from .llm.contracts import InferenceRequest

            aid = data["application_id"]
            app = flow.get(aid)
            if app["state"] not in (
                "scored",
                "selected",
                "preparing",
                "prepared",
            ) or not app["payload"].get("form_plan"):
                raise ValueError(
                    "Form reevaluation no longer matches the application step"
                )
            if app["state"] == "scored":
                service = InferenceService(self.db)
                result = await asyncio.to_thread(
                    service.run,
                    InferenceRequest(
                        task="fit_assessment",
                        candidate=app["candidate"],
                        application_id=aid,
                        question="Reevaluate the posting and the required questions introduced by the employer application form. Use the scoped confirmed answers as evidence. Preserve every central requirement and all rubric categories.",
                    ),
                    jid,
                    lambda value: self.checkpoint(jid, value),
                    lambda: self.cancelled(jid),
                )
                if self.cancelled(jid):
                    return {"cancelled": True}
                service.apply_score(result["run_id"])
            app = flow.get(aid)
            if not app["score"].get("primary"):
                return {
                    "state": app["state"],
                    "blocked_on": "fit",
                    "message": "The new employer requirements did not pass the recommendation gates",
                }
            # Existing attachment versions remain unchanged and are checked again below.
            flow.review(aid)
            if app["state"] == "scored":
                flow.select(aid)
            if flow.get(aid)["state"] == "selected":
                flow.transition(aid, "preparing", {"reuse_existing_package": True})
            if flow.get(aid)["state"] == "preparing":
                flow.transition(aid, "prepared")
            if self.cancelled(jid):
                return {"cancelled": True}
            next_job = enqueue(
                self.db,
                "fill",
                {"application_id": aid, "hunt_id": data.get("hunt_id")},
                max_attempts=1,
            )
            return {"state": "prepared", "next_job": next_job["job_id"]}
        if kind == "fill":
            result = await self.browser.fill(
                data["application_id"],
                jid,
                lambda value: self.checkpoint(jid, value),
                lambda: self.cancelled(jid),
            )
            app = flow.get(data["application_id"])
            if (
                result.get("state") == "scored"
                and result.get("reevaluate")
                and app["payload"].get("form_plan")
                and not self.cancelled(jid)
            ):
                next_job = enqueue(
                    self.db,
                    "form_fit",
                    {"application_id": app["id"], "hunt_id": data.get("hunt_id")},
                    max_attempts=2,
                )
                result["next_job"] = next_job["job_id"]
            return result
        if kind == "submit":
            return await self.browser.submit(data["application_id"])
        if kind == "reconcile":
            return await self.browser.reconcile(data["application_id"])
        if kind == "continue_verification":
            return await self.browser.greenhouse.continue_verification(
                data["application_id"]
            )
        raise ValueError("Unsupported job kind")

    async def run_once(self):
        await asyncio.to_thread(self.recover)
        job = await asyncio.to_thread(self.claim)
        if not job:
            return False
        heartbeat = asyncio.create_task(self.heartbeat(job["id"]))
        try:
            result = await self.dispatch(job)
            if result.get("yield") and not self.cancelled(job["id"]):
                self.db.execute(
                    "UPDATE jobs SET status='queued',checkpoint=%s,attempts=greatest(0,attempts-1),available_at=now()+interval '2 seconds',lease_until=NULL,updated_at=now() WHERE id=%s AND worker=%s",
                    (js(result), job["id"], self.id),
                )
                return True
            status = "cancelled" if self.cancelled(job["id"]) else "complete"
            self.db.execute(
                "UPDATE jobs SET status=%s,result=%s,error=NULL,checkpoint=CASE WHEN checkpoint->>'stage' IN ('failed','cancelled') THEN checkpoint || '{\"stage\":\"Task finished from the saved checkpoint\"}'::jsonb ELSE checkpoint END,lease_until=NULL,updated_at=now() WHERE id=%s AND worker=%s",
                (status, js(result), job["id"], self.id),
            )
            self.db.audit("job." + status, job["id"], {"kind": job["kind"]})
        except Exception as exc:
            if self.cancelled(job["id"]):
                self.db.execute(
                    "UPDATE jobs SET status='cancelled',lease_until=NULL,updated_at=now() WHERE id=%s AND worker=%s",
                    (job["id"], self.id),
                )
                return True
            # Exceptions are summarized without form values, page text or credentials.
            message = (
                str(exc)[:240] if isinstance(exc, ValueError) else type(exc).__name__
            )
            retry = (
                job["attempts"] < job["max_attempts"]
                and job["kind"] not in ("submit", "fill", "continue_verification")
                and not isinstance(exc, ValueError)
            )
            self.db.execute(
                "UPDATE jobs SET status=%s,error=%s,available_at=now()+(%s*interval '1 second'),lease_until=NULL,updated_at=now() WHERE id=%s AND worker=%s",
                (
                    "queued" if retry else "failed",
                    message,
                    2 ** job["attempts"],
                    job["id"],
                    self.id,
                ),
            )
            self.db.audit(
                "job.error", job["id"], {"error_type": message, "retry": retry}
            )
            # Local stderr is useful for programming failures; never print user input or page content.
            print(f"Job {job['id']} ({job['kind']}) failed: {message}", flush=True)
            if not isinstance(exc, ValueError):
                traceback.print_tb(exc.__traceback__)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        return True

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: setattr(self, "stop", True))
        try:
            while not self.stop:
                if not await self.run_once():
                    await asyncio.sleep(0.5)
        finally:
            await self.browser.close()
