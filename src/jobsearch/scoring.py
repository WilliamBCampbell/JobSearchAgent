from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Literal
from pydantic import BaseModel, Field
from .tracker import normalize
from .form_policy import experience_narrative

SCORING_POLICY = "2026-10-20.remote-pay.1"

ENGINEERING = {
    "technical": 35,
    "delivery": 25,
    "seniority": 15,
    "domain": 10,
    "ohio": 10,
    "compensation": 5,
}
SOLUTIONS = {
    "customer": 25,
    "technical": 25,
    "delivery": 15,
    "domain": 10,
    "seniority": 10,
    "ohio": 10,
    "compensation": 5,
}


class Requirement(BaseModel):
    text: str
    source: str
    category: str
    classification: Literal["required", "preferred"]
    central: bool = False
    material: bool = True
    assessment: Literal["strong", "partial", "unconfirmed", "gap", "hard_stop"]
    fact_ids: list[str] = Field(default_factory=list)
    deduction: float = Field(ge=0)
    reason: str


class Assessment(BaseModel):
    track: Literal["software", "applied_ai", "solutions"]
    requirements: list[Requirement]
    ohio: Literal["confirmed", "us_remote_unspecified", "unknown", "excluded"]
    compensation: dict = Field(
        default_factory=lambda: {
            "base": None,
            "bonus": None,
            "commission": None,
            "equity": None,
            "ote": None,
            "classification": "unknown",
        }
    )
    exceptional_low_pay_reason: str | None = None


def commitment_requirement(text):
    import re

    return bool(
        re.search(
            r"\b(travel|relocat\w*|on[- ]?site|hybrid|commut\w*|time[- ]?zone|schedule|hours|clearance|citizenship)\b",
            text,
            re.I,
        )
    )


def ranking_score(score):
    return score["total"] + score.get("preference_bonus", 0)


def evaluate(data: Assessment, official: dict, confirmed_fact_ids: set[str]):
    weights = SOLUTIONS if data.track == "solutions" else ENGINEERING
    scores = dict(weights)
    gaps = []
    matrix = []
    hard = []
    if set(r.category for r in data.requirements) != set(weights):
        raise ValueError("The matrix must cover every rubric category")
    for req in data.requirements:
        if req.category not in weights:
            raise ValueError("Unknown rubric category")
        if not req.source or not req.reason:
            raise ValueError("Requirements need a source and assessment reason")
        row = req.model_dump()
        if (
            req.assessment == "strong"
            and req.category not in ("ohio", "compensation")
            and (not req.fact_ids or set(req.fact_ids) - confirmed_fact_ids)
        ):
            row["assessment"] = "unconfirmed"
            row["reason"] = "Confirmed candidate evidence is missing. " + row["reason"]
        # Missing employer details remain unknown evidence, not candidate gaps.
        informational = row["assessment"] in ("unconfirmed", "partial") and (
            req.category == "compensation"
            and data.compensation.get("classification", "unknown") == "unknown"
            and not data.compensation.get("base")
            or req.category == "ohio"
            and data.ohio == "us_remote_unspecified"
            and not commitment_requirement(req.text)
        )
        if informational:
            row.update(material=False, central=False, deduction=0)
            row["policy_note"] = (
                "Pay is not listed. Use the candidate's application-specific expectations when asked."
                if req.category == "compensation"
                else "Remote work is acceptable without an explicit Ohio listing."
            )
        if not req.central and experience_narrative({"label": req.text}):
            row.update(
                material=False, central=False, deduction=0, application_question=True
            )
            row["policy_note"] = (
                "Prepare an evidence-backed application answer; ask the candidate for any missing details."
            )
        scores[req.category] -= row["deduction"]
        if (
            row["assessment"] in ("unconfirmed", "gap", "hard_stop")
            or (
                row["assessment"] == "partial"
                and req.central
                and req.classification == "required"
            )
        ) and row["material"]:
            gaps.append(row)
        if row["assessment"] == "hard_stop" and req.classification == "required":
            hard.append(req.text)
        matrix.append(row)
    scores = {k: max(0, v) for k, v in scores.items()}
    scores["ohio"] = min(
        scores["ohio"],
        {"confirmed": 10, "us_remote_unspecified": 10, "unknown": 4, "excluded": 0}[
            data.ohio
        ],
    )
    comp = data.compensation
    classification = comp.get("classification", "unknown")
    scores["compensation"] = min(
        scores["compensation"],
        {"unknown": 5, "ambiguous": 3, "base": 5}.get(classification, 2),
    )
    base = comp.get("base") or {}
    high = base.get("max") if isinstance(base, dict) else None
    if high is not None and high < 80000 and not data.exceptional_low_pay_reason:
        hard.append("Expected base below $80,000 without an exceptional-fit rationale")
    if data.ohio == "excluded":
        hard.append("Ohio explicitly excluded")
    verified = False
    if (
        official.get("open")
        and official.get("application_url")
        and official.get("verified_at")
    ):
        verified = datetime.fromisoformat(
            official["verified_at"].replace("Z", "+00:00")
        ) > datetime.now(timezone.utc) - timedelta(hours=24)
    central = [r for r in gaps if r["central"] and r["classification"] == "required"]
    unconfirmed = [r for r in gaps if r["assessment"] == "unconfirmed"]
    total = sum(scores.values())
    minima = data.track == "solutions" or (
        scores["technical"] >= 26 and scores["seniority"] >= 11
    )
    primary = (
        total >= 80
        and minima
        and verified
        and not central
        and len(unconfirmed) < 2
        and not hard
        and data.ohio != "unknown"
    )
    reasons = hard + [f"Unresolved central requirement: {r['text']}" for r in central]
    if not verified:
        reasons.append(
            "Official opening and application route need current verification"
        )
    if len(unconfirmed) >= 2:
        reasons.append("At least two material requirements remain unconfirmed")
    if not minima:
        reasons.append("Technical or seniority minimum not met")
    if data.ohio == "unknown":
        reasons.append("U.S. hiring eligibility is unknown")
    return {
        "scoring_policy": SCORING_POLICY,
        "track": data.track,
        "total": total,
        "scores": scores,
        "weights": weights,
        "deductions": {k: weights[k] - scores[k] for k in weights},
        "requirements": matrix,
        "gaps": gaps,
        "primary": primary,
        "band": "primary"
        if primary
        else "skip"
        if hard or total < 70
        else "conditional",
        "reasons": reasons,
        "ohio": data.ohio,
        "compensation": comp,
        "notice": "Scores are judgments, not hiring probabilities.",
    }


def duplicate_groups(rows: list[dict], prior: list[dict]):
    groups = []
    for row in rows:
        employer = normalize(row.get("company", row.get("employer", "")))
        role = normalize(row.get("title", row.get("role", "")))
        req = str(row.get("requisition") or "")
        exact = next(
            (
                g
                for g in groups
                if normalize(g["company"]) == employer
                and req
                and g.get("requisition") == req
            ),
            None,
        )
        if not exact:
            exact = next(
                (
                    g
                    for g in groups
                    if normalize(g["company"]) == employer
                    and not req
                    and not g.get("requisition")
                    and normalize(g["title"]) == role
                    and g.get("job_url") == row.get("job_url")
                ),
                None,
            )
        source = {
            "source": row.get("site", "unknown"),
            "url": row.get("job_url"),
            "query": row.get("search_term"),
        }
        if exact:
            if source not in exact["sources"]:
                exact["sources"].append(source)
            if not exact.get("description") and row.get("description"):
                exact["description"] = row["description"]
            continue
        match = []
        for p in prior:
            if normalize(p.get("employer", p.get("company", ""))) != employer:
                continue
            p_req = p.get("requisition", "")
            p_role = normalize(p.get("role", p.get("title", "")))
            if req and p_req and not p_req.startswith("legacy-"):
                if req == p_req:
                    match.append(
                        {
                            "id": p.get("id"),
                            "kind": "exact",
                            "status": p.get("state", p.get("status")),
                        }
                    )
            elif role == p_role or role in p_role or p_role in role:
                match.append(
                    {
                        "id": p.get("id"),
                        "kind": "ambiguous",
                        "status": p.get("state", p.get("status")),
                    }
                )
        groups.append(
            row
            | {
                "company": row.get("company", row.get("employer", "")),
                "title": row.get("title", row.get("role", "")),
                "sources": [source],
                "prior_matches": match,
                "duplicate_review": bool(match),
            }
        )
    for group in groups:
        for other in groups:
            if group is other:
                continue
            if (
                normalize(group["company"]) == normalize(other["company"])
                and normalize(group["title"]) == normalize(other["title"])
                and (not group.get("requisition") or not other.get("requisition"))
            ):
                group["duplicate_review"] = True
    return groups


def shortlist(rows):
    return sorted(
        [
            r
            for r in rows
            if r.get("score", {}).get("primary") and not r.get("duplicate_review")
        ],
        key=lambda r: (
            -ranking_score(r["score"]),
            len(r["score"].get("gaps", [])),
            -r["score"]["scores"]["ohio"],
            -r["score"]["scores"]["compensation"],
        ),
    )[:5]
