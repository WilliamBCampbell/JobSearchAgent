"""Candidate decisions about specific employer requirements, scoped to one application."""

from typing import Literal
from pydantic import BaseModel, Field, ConfigDict
from .config import digest
from .db import uid, js
from .memory import FactInput
from .scoring import Assessment, evaluate


class RequirementDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement_key: str = Field(min_length=64, max_length=64)
    score_hash: str = Field(min_length=64, max_length=64)
    decision: Literal["confirm", "deny"]
    note: str = Field(default="", max_length=2000)


def requirement_key(row):
    return digest(
        {
            k: row.get(k)
            for k in (
                "text",
                "source",
                "category",
                "classification",
                "central",
                "material",
            )
        }
    )


class RequirementDecisions:
    EDITABLE = {
        "scored",
        "selected",
        "prepared",
        "missing_answers",
        "review",
        "authorized",
        "manual_handoff",
    }

    def __init__(self, db):
        self.db = db

    def current(self, aid, conn=None):
        sql = (
            "SELECT d.* FROM requirement_decisions d JOIN facts f ON f.id=d.fact_id "
            "JOIN applications a ON a.id=d.application_id "
            "WHERE d.application_id=%s AND f.active AND f.status='confirmed' "
            "AND f.scope=a.id AND f.candidate=a.candidate ORDER BY d.created_at,d.id"
        )
        return (
            conn.execute(sql, (aid,)).fetchall() if conn else self.db.all(sql, (aid,))
        )

    def view(self, app):
        from .forms import non_qualification_field

        contacts = {
            f["label"]
            for f in app["payload"].get("form_schema", [])
            if non_qualification_field(f)
        }
        saved = {
            d["requirement_key"]: d
            for d in app["score"].get("requirement_decisions", [])
        }
        return {
            "score_hash": digest(app["score"]),
            "contact_fields": sorted(contacts),
            "needs_contact_cleanup": any(
                r["text"] in contacts and r.get("material", True)
                for r in app["score"].get("requirements", [])
            ),
            "editable": not app["imported"]
            and not app["paused"]
            and app["state"] in self.EDITABLE,
            "requirements": [
                r
                | {"key": requirement_key(r), "decision": saved.get(requirement_key(r))}
                for r in app["score"].get("requirements", [])
                if (
                    r.get("central")
                    and r.get("classification") == "required"
                    or r.get("category") not in ("ohio", "compensation")
                    and r.get("assessment") == "unconfirmed"
                    or requirement_key(r) in saved
                )
                and r["text"] not in contacts
                and r.get("material", True)
                and (
                    r.get("assessment")
                    in ("partial", "unconfirmed", "gap", "hard_stop")
                    or requirement_key(r) in saved
                )
            ],
        }

    def evaluate(self, app, assessment, confirmed, conn=None):
        records = self.current(app["id"], conn)
        decisions = {d["requirement_key"]: d for d in records}
        # Decision facts support exactly the requirement confirmed, never other claims.
        sql = "SELECT id FROM facts WHERE candidate=%s AND key LIKE 'requirement_decision_%%'"
        rows = (
            conn.execute(sql, (app["candidate"],)).fetchall()
            if conn
            else self.db.all(sql, (app["candidate"],))
        )
        decision_facts = {f["id"] for f in rows}
        updated, applied = [], []
        from .forms import non_qualification_field

        contacts = {
            f["label"]
            for f in app["payload"].get("form_schema", [])
            if non_qualification_field(f)
        }
        for req in assessment.requirements:
            row = req.model_dump()
            if row["text"] in contacts:
                # Keep the original row in the audit history; current fit must not
                # turn an administrative field into a career qualification.
                row.update(
                    central=False,
                    material=False,
                    deduction=0,
                    reason="This form field is checked during answer preparation.",
                )
            row["fact_ids"] = [f for f in row["fact_ids"] if f not in decision_facts]
            d = decisions.get(requirement_key(row))
            if d:
                confirm = d["decision"] == "confirm"
                row.update(
                    assessment="strong"
                    if confirm
                    else (
                        "hard_stop"
                        if d["requirement"]["assessment"] == "hard_stop"
                        else "gap"
                    ),
                    fact_ids=[d["fact_id"]],
                    deduction=0 if confirm else d["requirement"]["deduction"],
                    reason="Candidate confirmed meeting this requirement."
                    if confirm
                    else "Candidate confirmed they do not meet this requirement.",
                )
                applied.append(
                    {
                        k: d[k]
                        for k in (
                            "id",
                            "requirement_key",
                            "decision",
                            "note",
                            "fact_id",
                        )
                    }
                )
            updated.append(row)
        data = Assessment.model_validate(
            assessment.model_dump() | {"requirements": updated}
        )
        result = evaluate(data, app["official"], confirmed)
        proximity_sql = "SELECT id,value FROM facts WHERE candidate=%s AND scope=%s AND key='employer_driving_distance_miles' AND active AND status='confirmed'"
        proximity = (
            conn.execute(proximity_sql, (app["candidate"], app["id"])).fetchall()
            if conn
            else self.db.all(proximity_sql, (app["candidate"], app["id"]))
        )
        # A global distance or conflicting distances cannot establish this office's proximity.
        distances = {f["value"] for f in proximity if type(f["value"]) in (int, float)}
        nearby = (
            len(distances) == 1
            and all(type(f["value"]) in (int, float) for f in proximity)
            and 0 <= next(iter(distances)) <= 100
        )
        result["preference_bonus"] = 2 if nearby else 0
        result["proximity_fact_ids"] = [f["id"] for f in proximity] if nearby else []
        result["requirement_decisions"] = applied
        result["exceptional_low_pay_reason"] = data.exceptional_low_pay_reason
        denied = {
            d["requirement"]["text"]
            for d in records
            if d["decision"] == "deny"
            and d["requirement_key"] in {x["requirement_key"] for x in applied}
        }
        result["reasons"] = [
            reason.replace(
                "Unresolved central requirement: ", "Confirmed qualification gap: ", 1
            )
            if reason.removeprefix("Unresolved central requirement: ") in denied
            else reason
            for reason in result["reasons"]
        ]
        return result

    def decide(self, aid, request: RequirementDecisionInput):
        from .workflow import Workflow

        flow = Workflow(self.db)
        with self.db.tx() as c:
            app = flow.get(aid, c)
            flow.editable(app)
            if app["paused"] or app["state"] not in self.EDITABLE:
                raise ValueError(
                    "Requirement decisions need an active fit assessment before submission"
                )
            if c.execute(
                "SELECT 1 FROM attempts WHERE application_id=%s", (aid,)
            ).fetchone():
                raise ValueError(
                    "Attempted applications cannot change requirement decisions"
                )
            if digest(app["score"]) != request.score_hash:
                raise ValueError(
                    "The fit assessment changed. Refresh it before deciding"
                )
            matches = [
                r
                for r in self.view(app)["requirements"]
                if r["key"] == request.requirement_key
            ]
            if len(matches) != 1:
                raise ValueError(
                    "Choose a requirement needing review from this assessment"
                )
            row = {k: v for k, v in matches[0].items() if k not in ("key", "decision")}
            old = [
                d
                for d in self.current(aid, c)
                if d["requirement_key"] == request.requirement_key
            ]
            # Preserve the original deduction when changing Confirm back to Deny.
            original = old[-1]["requirement"] if old else row
            did, fid = uid(), uid()
            fact = FactInput(
                candidate=app["candidate"],
                kind="decision",
                key="requirement_decision_" + request.requirement_key,
                value={
                    "requirement": row["text"],
                    "meets_requirement": request.decision == "confirm",
                    "note": request.note.strip(),
                },
                status="confirmed",
                scope=aid,
                provenance={
                    "type": "user_confirmation",
                    "decision_id": did,
                    "requirement_key": request.requirement_key,
                    "source": row["source"],
                },
            )
            old_ids = [d["fact_id"] for d in old]
            c.execute(
                "UPDATE facts SET active=false,status='superseded' WHERE id=ANY(%s)",
                (old_ids,),
            )
            for old_id in old_ids:
                c.execute(
                    "UPDATE answer_cache SET valid=false WHERE fact_ids @> %s",
                    (js([old_id]),),
                )
            c.execute(
                "INSERT INTO facts(id,candidate,kind,key,value,status,scope,provenance,supersedes) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    fid,
                    fact.candidate,
                    fact.kind,
                    fact.key,
                    js(fact.value),
                    fact.status,
                    aid,
                    js(fact.provenance),
                    old_ids[-1] if old_ids else None,
                ),
            )
            c.execute(
                "INSERT INTO requirement_decisions(id,application_id,requirement_key,requirement,decision,note,fact_id,score_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    did,
                    aid,
                    request.requirement_key,
                    js(original),
                    request.decision,
                    request.note.strip(),
                    fid,
                    request.score_hash,
                ),
            )
            confirmed = {
                f["id"]
                for f in c.execute(
                    "SELECT id FROM facts WHERE candidate=%s AND active AND status='confirmed' AND scope=ANY(%s)",
                    (app["candidate"], ["global", aid]),
                ).fetchall()
            }
            data = Assessment.model_validate(
                {
                    k: app["score"][k]
                    for k in Assessment.model_fields
                    if k in app["score"]
                }
            )
            score = self.evaluate(app, data, confirmed, c)
            if app["score"].get("evidence_changed"):
                score.update(evidence_changed=True, primary=False)
                score["reasons"].append(
                    "The official posting changed; rescore against the current evidence"
                )
            c.execute(
                "UPDATE applications SET score=%s,payload=payload || '{\"browser_hash\":null}'::jsonb,revision=revision+1,updated_at=now() WHERE id=%s",
                (js(score), aid),
            )
            c.execute(
                "UPDATE approvals SET invalidated=true WHERE application_id=%s", (aid,)
            )
            if app["state"] == "authorized":
                flow.transition(aid, "review", conn=c)
            # Decisions invalidate browser review, not saved preparation steps.
            self.db.audit(
                "requirement.decided",
                aid,
                {
                    "decision_id": did,
                    "requirement_key": request.requirement_key,
                    "decision": request.decision,
                },
                c,
            )
        return flow.get(aid)
