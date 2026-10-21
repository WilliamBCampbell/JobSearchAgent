import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jobsearch.forms import FormSpec, FieldSpec
from jobsearch.greenhouse_transport import validate_application, GreenhouseTransport


def native_request():
    spec = FormSpec(
        adapter="greenhouse",
        url="https://job-boards.greenhouse.io/example/jobs/1",
        requisition="1",
        fields=[
            FieldSpec(
                key="email", name="email", label="Email", type="email", required=True
            )
        ],
    )
    payload = {"answers": {"email": {"value": "alex@example.test"}}, "attachments": []}
    body = {
        "fingerprint": "synthetic-fingerprint",
        "g-recaptcha-enterprise-token": "synthetic-private-token",
        "job_application": {
            "email": "alex@example.test",
            "answers_attributes": {},
            "attachments": {},
            "data_compliance": {},
            "demographic_answers": [],
            "employments": [
                {
                    "start_date": {"month": None, "year": None},
                    "end_date": {"month": None, "year": None},
                    "current": False,
                }
            ],
            "educations": [
                {
                    "school_name_id": None,
                    "degree_id": None,
                    "discipline_id": None,
                    "start_date": {"month": None, "year": None},
                    "end_date": {"month": None, "year": None},
                }
            ],
            "mapped_url_token": None,
            "appcast_click_id": None,
            "time_zone": "America/New_York",
            "from_job_board_renderer": True,
        },
    }
    return spec, payload, body


def test_native_blank_history_is_not_a_career_claim_or_consent():
    spec, payload, body = native_request()
    errors = []
    assert validate_application(
        json.dumps(body).encode(), spec, payload, {}, {}, errors
    )
    assert not errors
    for value in (True, False):
        changed = deepcopy(body)
        changed["job_application"]["data_compliance"] = {"extra_consent": value}
        assert not validate_application(
            json.dumps(changed).encode(), spec, payload, {}, {}
        )


@pytest.mark.parametrize(
    "row",
    [
        {"current": True},
        {"current": "false"},
        {"current": 0},
        {"company_name": "Unapproved Company", "current": False},
        {"title": "Unapproved Role", "current": False},
        {"start_date": {"year": "2020"}, "current": False},
        {"end_date": {"month": "1"}, "current": False},
        {"start_date": {"extra": None}, "current": False},
        {"extra_field": None, "current": False},
    ],
)
def test_native_history_must_be_blank_and_match_known_structure(row):
    spec, payload, body = native_request()
    body["job_application"]["employments"] = [row]
    errors = []
    assert not validate_application(
        json.dumps(body).encode(), spec, payload, {}, {}, errors
    )
    assert errors == ["unreviewed_employment"]


async def test_blocked_request_reports_reason_without_logging_answers_or_tokens():
    spec, payload, body = native_request()
    body["job_application"]["employments"][0]["company_name"] = "Private employer value"
    events = []
    db = SimpleNamespace(
        settings=SimpleNamespace(
            token=lambda: "local-fixture-token", origin="http://127.0.0.1:8766"
        ),
        execute=lambda *args: events.append(args),
        audit=lambda *args: events.append(args),
    )
    transport = GreenhouseTransport(
        db, {"id": "fixture", "candidate": "fixture"}, spec, {"payload": payload}, {}
    )
    transport.check = lambda: None
    transport.attempt = "fixture-attempt"
    route = SimpleNamespace(
        request=SimpleNamespace(
            url=spec.url,
            method="POST",
            headers={},
            post_data_buffer=json.dumps(body).encode(),
        ),
        abort=AsyncMock(),
        continue_=AsyncMock(),
    )
    assert await transport.handle(route, {"phase": "send"})
    route.abort.assert_awaited_once_with("blockedbyclient")
    route.continue_.assert_not_awaited()
    assert not transport.posted
    assert transport.stage == "submission_blocked"
    assert "No final application request was sent" in transport.error
    assert "unreviewed_employment" in repr(events)
    for private in (
        "Private employer value",
        "synthetic-private-token",
        "alex@example.test",
    ):
        assert private not in repr(events)
        assert private not in transport.error
