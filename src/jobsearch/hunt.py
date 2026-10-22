"""Checkpointed search coordinator. Each tick yields so child jobs can run."""

import asyncio
from typing import Annotated, Literal
from pydantic import BaseModel, Field, StringConstraints
from .db import js, uid
from .config import digest
from .jobspy_client import JobSpyClient
from .queue import enqueue
from .workflow import Workflow
from .llm.service import InferenceService
from .llm.contracts import InferenceRequest
from .openings import OfficialOpenings

TERMS = {
    "software": [
        ".NET software engineer",
        "senior C# developer",
        "backend software engineer .NET",
    ],
    "applied_ai": [
        "applied AI engineer",
        "AI integration engineer",
        "AI agent engineer",
    ],
    "solutions": ["solutions engineer", "customer engineer", "implementation engineer"],
}
TERMINAL = {"complete", "failed", "cancelled", "unresolved"}


class HuntRequest(BaseModel):
    candidate: Literal["real", "fixture"] = "real"
    role: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
        ]
        | None
    ) = None
    track: Literal["software", "applied_ai", "solutions"] = "software"
    results_per_query: int = Field(default=20, ge=5, le=50)
    evaluation_limit: int = Field(default=12, ge=1, le=30)
    discovery_limit: int = Field(default=60, ge=1, le=200)
    primary_limit: int = Field(default=5, ge=1, le=5)


class Hunt:
    def __init__(self, db):
        self.db = db
        self.mcp = JobSpyClient(db)
        self.flow = Workflow(db)
        self.inference = InferenceService(db)

    def start(self, request):
        if request.candidate == "real":
            if not (
                self.db.settings.live_search or self.db.setting("live_search", False)
            ):
                raise ValueError("Enable online searches before starting the agent")
            if (
                self.inference.config.backend != "codex_exec"
                or not self.inference.config.enabled
            ):
                raise ValueError(
                    "Enable Codex evaluation before starting a real search"
                )
        with self.db.tx() as c:
            c.execute("SELECT pg_advisory_xact_lock(917321)")
            old = c.execute(
                "SELECT id FROM hunts WHERE candidate=%s AND status IN ('running','paused')",
                (request.candidate,),
            ).fetchone()
            if old:
                return self.get(old["id"])
            hid = uid()
            c.execute(
                "INSERT INTO hunts(id,candidate,request) VALUES(%s,%s,%s)",
                (hid, request.candidate, js(request.model_dump())),
            )
            c.execute(
                "INSERT INTO jobs(id,kind,input,max_attempts) VALUES(%s,'hunt',%s,3)",
                (uid(), js({"hunt_id": hid})),
            )
        self.db.audit("hunt.started", hid, request.model_dump())
        return self.get(hid)

    def get(self, hid):
        hunt = self.db.one("SELECT * FROM hunts WHERE id=%s", (hid,))
        if not hunt:
            raise ValueError("Search run not found")
        hunt["leads"] = self.db.all(
            "SELECT lead.*,a.employer,a.role,a.requisition,a.score,a.state,a.official,a.payload FROM hunt_leads lead LEFT JOIN applications a ON a.id=lead.application_id WHERE lead.hunt_id=%s ORDER BY lead.ordinal",
            (hid,),
        )
        hunt["jobs"] = self.db.all(
            "SELECT id,kind,status,checkpoint,error FROM jobs WHERE input->>'hunt_id'=%s OR id IN (SELECT child_job FROM hunt_leads WHERE hunt_id=%s)",
            (hid, hid),
        )
        return hunt

    def control(self, hid, action):
        hunt = self.get(hid)
        if action not in ("pause", "resume", "cancel"):
            raise ValueError("Unsupported search control")
        if hunt["status"] in ("complete", "cancelled"):
            raise ValueError("This search run has finished")
        status = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[action]
        self.db.execute(
            "UPDATE hunts SET status=%s,updated_at=now() WHERE id=%s", (status, hid)
        )
        if action == "cancel":
            ids = [x["child_job"] for x in hunt["leads"] if x["child_job"]]
            if hunt["checkpoint"].get("search_id"):
                ids.append(hunt["checkpoint"]["search_id"])
            self.db.execute(
                "UPDATE jobs SET cancel_requested=true,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=ANY(%s) OR input->>'hunt_id'=%s",
                (ids, hid),
            )
        if action == "resume":
            self.db.execute(
                "UPDATE jobs SET status='queued',attempts=0,error=NULL,available_at=now() WHERE kind='hunt' AND input->>'hunt_id'=%s AND status<>'running'",
                (hid,),
            )
        self.db.audit("hunt." + action, hid)
        return self.get(hid)

    def save(self, hid, cp):
        self.db.execute(
            "UPDATE hunts SET checkpoint=%s,updated_at=now() WHERE id=%s", (js(cp), hid)
        )

    def lead(self, lid, status, reason=None, **values):
        allowed = {"application_id", "child_job", "evidence"}
        if set(values) - allowed:
            raise ValueError("Invalid lead update")
        fields = ["status=%s", "reason=%s", "updated_at=now()"]
        args = [status, reason]
        for key, value in values.items():
            fields.append(key + "=%s")
            args.append(js(value) if key == "evidence" else value)
        self.db.execute(
            "UPDATE hunt_leads SET "
            + ",".join(fields)
            + " WHERE id=%s AND status<>'discarded'",
            (*args, lid),
        )

    def launch(self, lead, task, candidate):
        # Active child reuse and the inference cache make retries idempotent.
        existing = self.db.one(
            "SELECT * FROM jobs WHERE kind='llm' AND input->>'application_id'=%s AND input->>'task'=%s ORDER BY created_at DESC LIMIT 1",
            (lead["application_id"], task),
        )
        job = existing or self.inference.start(
            InferenceRequest(
                task=task, candidate=candidate, application_id=lead["application_id"]
            )
        )
        self.lead(lead["id"], task, child_job=job.get("job_id", job.get("id")))

    def repair(self, lead, task, candidate, error, allowed=True):
        retries = lead["evidence"].get("evaluation_retries", {})
        if allowed and not retries.get(task):
            retry = self.inference.start(
                InferenceRequest(
                    task=task,
                    candidate=candidate,
                    application_id=lead["application_id"],
                    question="Repair the previous rejected evaluation using current evidence. Validation error: "
                    + error[:240]
                    + ". Use only supplied source IDs and rubric category names. Cover every rubric category when assessing fit; preserve every mandatory qualification.",
                )
            )
            self.lead(
                lead["id"],
                task,
                child_job=retry["job_id"],
                evidence=lead["evidence"] | {"evaluation_retries": retries | {task: 1}},
            )
        else:
            self.lead(lead["id"], "evaluation_failed", error)

    async def tick(self, job):
        hid = job["input"]["hunt_id"]
        hunt = self.get(hid)
        if hunt["status"] == "cancelled":
            return {"cancelled": True}
        if hunt["status"] == "paused":
            return {"yield": True, "stage": "Paused"}
        cp = hunt["checkpoint"]
        req = HuntRequest(**hunt["request"])
        if not cp.get("search_id"):
            search = await self.mcp.call(
                "start_search",
                {
                    "request": {
                        "request_key": "hunt:" + hid,
                        "mode": "live" if req.candidate == "real" else "fixture",
                        "track": req.track,
                        "profile": "solutions"
                        if req.track == "solutions"
                        else "software_dev",
                        "terms": [req.role] if req.role else TERMS[req.track],
                        "results": req.results_per_query,
                    }
                },
            )
            cp.update(
                search_id=search["job_id"], stage="Searching job boards through JobSpy"
            )
            self.save(hid, cp)
            return {"yield": True, "stage": cp["stage"]}
        if not cp.get("discovery_loaded"):
            progress = await self.mcp.call(
                "search_progress", {"job_id": cp["search_id"]}
            )
            if progress["status"] not in TERMINAL:
                return {"yield": True, "stage": "Searching job boards through JobSpy"}
            rows = []
            offset = 0
            while True:
                page = await self.mcp.call(
                    "search_results",
                    {"job_id": cp["search_id"], "offset": offset, "limit": 100},
                )
                rows.extend(page["results"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            cp["manifest"] = page["manifest"]
            cp["search_status"] = progress["status"]
            cp["search_error"] = progress.get("error")

            # Prioritize usable employer links and stack overlap, never fabricate scores.
            def priority(row):
                text = (
                    row.get("title", "") + " " + (row.get("description") or "")
                ).lower()
                terms = {
                    "software": [".net", "c#", "postgres", "vue"],
                    "applied_ai": ["python", "agent", "integration"],
                    "solutions": ["customer", "integration", "technical"],
                }[req.track]
                return (bool(row.get("job_url_direct")), sum(t in text for t in terms))

            rows.sort(key=priority, reverse=True)
            for i, row in enumerate(rows[: req.discovery_limit]):
                self.db.execute(
                    "INSERT INTO hunt_leads(id,hunt_id,ordinal,discovery) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (digest([hid, i]), hid, i, js(row)),
                )
            cp.update(
                discovery_loaded=True,
                discovered=len(rows),
                not_evaluated=max(0, len(rows) - req.discovery_limit),
                stage="Checking official openings and prior applications",
            )
            self.save(hid, cp)
            return {"yield": True, "stage": cp["stage"]}
        # One bounded unit per tick. The single worker can run queued child jobs.
        pending = next(
            (
                lead
                for lead in hunt["leads"]
                if lead["status"]
                in (
                    "discovered",
                    "verified",
                    "requirement_extraction",
                    "fit_assessment",
                )
            ),
            None,
        )
        if pending:
            lead = pending
            if lead["status"] == "discovered":
                evaluated = sum(bool(x["application_id"]) for x in hunt["leads"])
                if evaluated >= req.evaluation_limit:
                    self.lead(
                        lead["id"],
                        "budget_limit",
                        "Evaluation limit reached; available in this run's discovery record",
                    )
                elif lead["discovery"].get("duplicate_review"):
                    self.lead(
                        lead["id"],
                        "duplicate",
                        "Prior or ambiguous application match; preserved for review",
                    )
                else:
                    evidence = (
                        await asyncio.to_thread(
                            OfficialOpenings(self.db).verify, lead["discovery"]
                        )
                        if req.candidate == "real"
                        else await self.fixture_verify(lead)
                    )
                    if not evidence.get("open"):
                        self.lead(
                            lead["id"],
                            "unverified",
                            evidence.get("reason", "Official opening unavailable"),
                            evidence=evidence,
                        )
                    else:
                        try:
                            app = self.flow.create(
                                lead["discovery"]["company"],
                                evidence.get("title", lead["discovery"]["title"]),
                                evidence["requisition"],
                                req.track,
                                lead["discovery"].get("sources", []),
                                candidate=req.candidate,
                                discovery_key=lead["id"],
                            )
                            if app["state"] == "discovered":
                                self.flow.set_official(app["id"], evidence)
                            self.lead(
                                lead["id"],
                                "verified",
                                application_id=app["id"],
                                evidence=evidence,
                            )
                        except ValueError as exc:
                            self.lead(
                                lead["id"], "duplicate", str(exc), evidence=evidence
                            )
            elif lead["status"] == "verified":
                self.launch(lead, "requirement_extraction", req.candidate)
            else:
                child = self.db.one(
                    "SELECT * FROM jobs WHERE id=%s", (lead["child_job"],)
                )
                if child["status"] in TERMINAL:
                    if child["status"] != "complete":
                        self.repair(
                            lead,
                            lead["status"],
                            req.candidate,
                            child.get("error") or child["status"],
                            allowed=child["status"] == "failed",
                        )
                    elif lead["status"] == "requirement_extraction":
                        requirements = child["result"]["output"]["requirements"]
                        # Preserve extracted central requirements before independent fit assessment.
                        self.db.execute(
                            "UPDATE applications SET score=%s WHERE id=%s",
                            (
                                js({"requirements": requirements}),
                                lead["application_id"],
                            ),
                        )
                        self.launch(lead, "fit_assessment", req.candidate)
                    else:
                        try:
                            app = self.flow.get(lead["application_id"])
                            if app["state"] == "verified":
                                self.inference.apply_score(child["result"]["run_id"])
                                app = self.flow.get(lead["application_id"])
                            self.lead(
                                lead["id"],
                                "qualified"
                                if app["score"]["primary"]
                                else "not_qualified",
                                "; ".join(app["score"]["reasons"]) or None,
                            )
                        except ValueError as exc:
                            self.repair(lead, "fit_assessment", req.candidate, str(exc))
            cp["stage"] = "Verifying and evaluating openings"
            self.save(hid, cp)
            return {"yield": True, "stage": cp["stage"]}
        if not cp.get("shortlisted"):
            from .scoring import ranking_score

            ranked = sorted(
                (lead for lead in hunt["leads"] if lead["status"] == "qualified"),
                key=lambda lead: ranking_score(lead["score"]),
                reverse=True,
            )
            for i, lead in enumerate(ranked):
                if i >= req.primary_limit:
                    self.lead(
                        lead["id"],
                        "reserve",
                        "Qualified; outside this run's primary limit",
                    )
                    continue
                app = self.flow.get(lead["application_id"])
                if app["state"] == "scored":
                    self.flow.select(app["id"])
                child = enqueue(
                    self.db,
                    "package",
                    {"application_id": app["id"], "include_cover": False},
                )
                self.lead(lead["id"], "preparing", child_job=child["job_id"])
            cp.update(shortlisted=True, stage="Preparing the strongest applications")
            self.save(hid, cp)
            return {"yield": True, "stage": cp["stage"]}
        for lead in hunt["leads"]:
            if lead["status"] == "preparing":
                child = self.db.one(
                    "SELECT * FROM jobs WHERE id=%s", (lead["child_job"],)
                )
                if child["status"] not in TERMINAL:
                    return {"yield": True, "stage": "Preparing application documents"}
                from .progress import document_review_needed

                if (
                    child["status"] == "complete"
                    and req.candidate == "real"
                    and not document_review_needed(
                        self.flow.get(lead["application_id"])
                    )
                ):
                    form_job = enqueue(
                        self.db,
                        "fill",
                        {"application_id": lead["application_id"], "hunt_id": hid},
                        max_attempts=1,
                    )
                    self.lead(
                        lead["id"], "inspecting_form", child_job=form_job["job_id"]
                    )
                else:
                    self.lead(
                        lead["id"],
                        "ready"
                        if child["status"] == "complete"
                        else "preparation_failed",
                        child.get("error"),
                    )
                return {"yield": True, "stage": "Preparing application documents"}
        for lead in hunt["leads"]:
            if lead["status"] == "inspecting_form":
                child = self.db.one(
                    "SELECT * FROM jobs WHERE id=%s", (lead["child_job"],)
                )
                if child["status"] not in TERMINAL:
                    return {
                        "yield": True,
                        "stage": "Inspecting employer forms and preparing answers",
                    }
                if child.get("result", {}).get("next_job"):
                    self.lead(
                        lead["id"],
                        "inspecting_form",
                        child_job=child["result"]["next_job"],
                    )
                    return {
                        "yield": True,
                        "stage": "Reevaluating requirements introduced by the employer form",
                    }
                app = self.flow.get(lead["application_id"])
                self.lead(
                    lead["id"],
                    "ready"
                    if child["status"] == "complete"
                    and app["state"] in ("review", "missing_answers", "manual_handoff")
                    else "form_review_needed",
                    child.get("error"),
                )
                return {"yield": True, "stage": "Preparing application reviews"}
        ready = sum(lead["status"] == "ready" for lead in hunt["leads"])
        cp.update(
            stage="Ready for review" if ready else "No prepared recommendations",
            ready=ready,
        )
        self.save(hid, cp)
        self.db.execute(
            "UPDATE hunts SET status='complete',updated_at=now() WHERE id=%s", (hid,)
        )
        self.db.audit(
            "hunt.completed", hid, {"ready": ready, "discovered": cp["discovered"]}
        )
        return {"hunt_id": hid, "ready": ready, "stage": cp["stage"]}

    async def fixture_verify(self, lead):
        from .verification import Verification

        row = lead["discovery"]
        app = {
            "id": lead["id"],
            "candidate": "fixture",
            "requisition": row["requisition"],
        }
        url = self.db.settings.origin + "/fixture/posting/" + row["requisition"]
        return await asyncio.to_thread(
            Verification(self.db).verify, app, url, "fixture"
        )
