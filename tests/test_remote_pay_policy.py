from jobsearch.fixtures import seed, assessment
from jobsearch.scoring import evaluate, shortlist
from jobsearch.attention_summary import concerns
from jobsearch.requirement_decisions import RequirementDecisions
from jobsearch.memory import Memory, FactInput
from jobsearch.workflow import Workflow
from .test_scoring_search import official
from .test_llm import application


def remote_assessment(db):
    ids = set(seed(db)["facts"].values())
    data = assessment(db)
    data.ohio = "us_remote_unspecified"
    data.compensation = {"classification": "unknown", "base": None}
    for r in data.requirements:
        if r.category in ("ohio", "compensation"):
            r.text = (
                "Ohio eligibility is unspecified."
                if r.category == "ohio"
                else "Compensation is unspecified."
            )
            r.assessment = "unconfirmed"
            r.central = True
            r.material = True
            r.deduction = 3
    return data, ids


def test_remote_and_unlisted_pay_are_informational_not_gaps(db):
    data, ids = remote_assessment(db)
    score = evaluate(data, official(), ids)
    assert score["primary"]
    assert score["scores"]["ohio"] == 10
    assert score["scores"]["compensation"] == 5
    assert not score["gaps"] and not concerns(score)
    assert score["ohio"] == "us_remote_unspecified"
    assert score["compensation"]["base"] is None
    assert all(
        not r["material"] and not r["central"] and r["deduction"] == 0
        for r in score["requirements"]
        if r["category"] in ("ohio", "compensation")
    )


def test_remote_preference_does_not_waive_restrictions_or_commitments(db):
    data, ids = remote_assessment(db)
    data.ohio = "excluded"
    assert not evaluate(data, official(), ids)["primary"]
    data.ohio = "unknown"
    assert not evaluate(data, official(), ids)["primary"]
    data.ohio = "us_remote_unspecified"
    row = next(r for r in data.requirements if r.category == "ohio")
    row.text = "Travel to customers 50% of the time"
    score = evaluate(data, official(), ids)
    assert not score["primary"]
    assert any(r["text"] == row.text for r in score["gaps"])
    row.text = "Ohio eligibility is unspecified."
    data.compensation = {"classification": "base", "base": {"max": 65000}}
    assert not evaluate(data, official(), ids)["primary"]


def test_proximity_is_scoped_confirmed_and_cannot_waive_fit(db):
    app = application(db)
    flow, aid = Workflow(db), app["id"]
    Memory(db).put(
        FactInput(
            candidate="fixture",
            kind="preference",
            key="employer_driving_distance_miles",
            value=50,
            status="confirmed",
            scope=aid,
            provenance={"type": "user_confirmation"},
        )
    )
    score = flow.score(aid, assessment(db))["score"]
    assert score["preference_bonus"] == 2 and score["proximity_fact_ids"]
    other = flow.get(aid) | {"id": "other-application"}
    ids = {
        r["id"]
        for r in db.all("SELECT id FROM facts WHERE active AND status='confirmed'")
    }
    other_score = RequirementDecisions(db).evaluate(other, assessment(db), ids)
    assert other_score["preference_bonus"] == 0
    better = score | {"total": score["total"] + 1, "preference_bonus": 0}
    ranked = shortlist([{"score": better, "id": "far"}, {"score": score, "id": "near"}])
    assert ranked[0]["id"] == "near"
    blocked = flow.score(aid, assessment(db, mandatory_gap=True))["score"]
    assert blocked["preference_bonus"] == 2 and not blocked["primary"]
    assert not shortlist([{"score": blocked}])


def test_project_prompts_are_answers_while_posting_qualifications_still_count(db):
    from jobsearch.forms import greenhouse_form
    from jobsearch.scoring import Requirement

    data, ids = remote_assessment(db)
    prompts = [
        "Share your GitHub URL and identify 1–2 projects you've personally built or significantly contributed to. Briefly explain your specific contributions.",
        "Agentic AI Experience: In 2026, what agentic AI tools, workflows, or harness-driven solutions have you personally built or implemented? Please provide specific examples of your contributions and highlight what you would be able to demonstrate during a technical interview.",
    ]
    baseline = evaluate(data, official(), ids)
    for text in prompts:
        data.requirements.append(
            Requirement(
                text=text,
                source="official:fixture",
                category="technical",
                classification="required",
                central=False,
                assessment="partial",
                deduction=4,
                reason="Answer not provided",
            )
        )
    score = evaluate(data, official(), ids)
    assert (
        score["primary"]
        and score["scores"]["technical"] == baseline["scores"]["technical"]
    )
    assert len([r for r in score["requirements"] if r.get("application_question")]) == 2
    spec = greenhouse_form(
        {
            "questions": [
                {
                    "label": text,
                    "required": True,
                    "fields": [{"name": str(i), "type": "textarea"}],
                }
                for i, text in enumerate(
                    prompts
                    + ["Describe your minimum 5 years of professional C# experience"]
                )
            ]
        },
        "https://job-boards.greenhouse.io/demo/jobs/1",
        "1",
    )
    assert [f.qualification for f in spec.fields] == [False, False, True]
    assert all(f.required for f in spec.fields)
    data.requirements.append(
        Requirement(
            text="Production agentic AI implementation experience",
            source="official:fixture",
            category="technical",
            classification="required",
            central=True,
            assessment="unconfirmed",
            deduction=0,
            reason="Candidate evidence missing",
        )
    )
    assert not evaluate(data, official(), ids)["primary"]
