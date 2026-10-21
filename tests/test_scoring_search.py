from datetime import datetime, timezone
from jobsearch.fixtures import seed, assessment
from jobsearch.scoring import evaluate, duplicate_groups, shortlist
from jobsearch.search import SearchService, SearchRequest
from jobsearch.queue import Worker


def official():
    return {
        "open": True,
        "application_url": "https://example.invalid/apply",
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


def test_high_score_does_not_override_central_gap(db):
    facts = seed(db)["facts"]
    result = evaluate(
        assessment(db, mandatory_gap=True), official(), set(facts.values())
    )
    assert result["total"] >= 80 and not result["primary"]
    assert any("central" in r for r in result["reasons"])


def test_engineering_caps_and_two_unconfirmed_requirements(db):
    ids = set(seed(db)["facts"].values())
    a = assessment(db)
    a.ohio = "us_remote_unspecified"
    a.compensation = {"classification": "unknown"}
    a.requirements[1].assessment = "unconfirmed"
    a.requirements[2].assessment = "unconfirmed"
    r = evaluate(a, official(), ids)
    assert (
        r["scores"]["ohio"] == 10
        and r["scores"]["compensation"] == 5
        and not r["primary"]
    )


def test_solutions_has_its_own_weights(db):
    ids = set(seed(db)["facts"].values())
    r = evaluate(assessment(db, "solutions"), official(), ids)
    assert (
        r["weights"]["customer"] == 25
        and r["weights"]["seniority"] == 10
        and r["primary"]
    )


def test_dedup_preserves_distinct_requisitions_and_all_statuses():
    prior = [
        {
            "id": "1",
            "employer": "Example Co.",
            "role": "Engineer",
            "requisition": "A",
            "state": "rejected",
        },
        {
            "id": "2",
            "employer": "Example Co.",
            "role": "Engineer",
            "requisition": "B",
            "state": "deferred",
        },
    ]
    rows = [
        {
            "company": "Example Co",
            "title": "Engineer",
            "requisition": "A",
            "site": "indeed",
        },
        {
            "company": "Example Co.",
            "title": "Engineer",
            "requisition": "A",
            "site": "linkedin",
        },
        {
            "company": "Example Co",
            "title": "Engineer",
            "requisition": "C",
            "site": "indeed",
        },
    ]
    result = duplicate_groups(rows, prior)
    assert len(result) == 2 and len(result[0]["sources"]) == 2
    assert result[0]["prior_matches"][0]["status"] == "rejected"
    assert not result[1]["duplicate_review"]
    ambiguous = duplicate_groups(
        [{"company": "Example Co", "title": "Engineer"}], prior
    )
    assert ambiguous[0]["duplicate_review"]


def test_no_padding_in_shortlist(db):
    ids = set(seed(db)["facts"].values())
    good = evaluate(assessment(db), official(), ids)
    bad = evaluate(assessment(db, mandatory_gap=True), official(), ids)
    assert len(shortlist([{"score": good}, {"score": bad}])) == 1
    assert len(shortlist([{"score": good} for _ in range(10)])) == 5


async def test_search_partial_failures_and_ignored_filters_survive(db):
    seed(db)
    service = SearchService(db)
    job = service.start(SearchRequest(terms=["API engineer"]))
    worker = Worker(db)
    await worker.run_once()
    await worker.browser.close()
    result = service.results(job["job_id"])
    assert result["status"] == "complete"
    assert result["manifest"]["status"] == "partial"
    linkedin = next(
        q for q in result["manifest"]["queries"] if q["source"] == "linkedin"
    )
    assert "is_remote" not in linkedin["effective_filters"]
    assert linkedin["requested_filters"]["is_remote"] is True
    assert (
        linkedin["errors"] == ["rate_limited"] and linkedin["missing_descriptions"] == 1
    )
    first = service.results(job["job_id"], 0, 1)
    assert first["next_offset"] == 1 and len(first["results"]) == 1


def test_cancel_preserves_durable_id(db):
    service = SearchService(db)
    job = service.start(SearchRequest())
    service.cancel(job["job_id"])
    assert service.progress(job["job_id"])["status"] == "cancelled"


def test_partial_central_required_qualification_is_not_primary(db):
    ids = set(seed(db)["facts"].values())
    data = assessment(db)
    data.requirements[0].assessment = "partial"
    result = evaluate(data, official(), ids)
    assert result["total"] >= 80 and not result["primary"]
