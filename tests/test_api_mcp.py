import os
import sys
from fastapi.testclient import TestClient
from jobsearch.api import create_app
from jobsearch.fixtures import seed
from jobsearch.queue import Worker
from mcp import Client
from mcp.client.stdio import StdioServerParameters


def test_api_auth_origin_and_path_protection(db):
    with TestClient(create_app(db.settings)) as client:
        assert client.get("/api/facts").status_code == 401
        headers = {"X-JSA-Token": db.settings.token()}
        assert client.get("/api/facts", headers=headers).status_code == 200
        assert (
            client.post(
                "/api/fixtures/seed",
                json={},
                headers=headers | {"Origin": "https://attacker.invalid"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/facts", headers=headers | {"Host": "attacker.invalid"}
            ).status_code
            == 400
        )
        assert (
            client.get("/api/artifacts/../../etc/passwd", headers=headers).status_code
            != 200
        )
        assert client.post("/api/session", json={}, headers=headers).status_code == 200
        assert client.get("/api/overview").status_code == 200


async def test_mcp_protocol_tools_and_durable_search(db):
    seed(db)
    env = os.environ | {
        "JSA_STATE": str(db.settings.state),
        "JSA_DATABASE_URL": db.settings.database,
        "JSA_WORKSPACE": str(db.settings.workspace),
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "jobsearch.mcp_server"], env=env
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        names = (
            {t.name for t in tools.tools}
            if hasattr(tools, "tools")
            else {t.name for t in tools}
        )
        assert {
            "health",
            "list_profiles",
            "start_search",
            "search_progress",
            "search_results",
            "cancel_search",
            "normalize_results",
            "export_submission_handoff",
        } <= names
        profiles = await client.call_tool("list_profiles", {})
        assert not profiles.is_error
        started = await client.call_tool(
            "start_search", {"request": {"terms": ["C# engineer"], "mode": "fixture"}}
        )
        assert not started.is_error
        result = started.structured_content
        if "result" in result:
            result = result["result"]
        worker = Worker(db)
        await worker.run_once()
        await worker.browser.close()
        page = await client.call_tool(
            "search_results", {"job_id": result["job_id"], "offset": 0, "limit": 1}
        )
        assert not page.is_error
        structured = page.structured_content
        if "result" in structured:
            structured = structured["result"]
        assert structured["total"] >= 2 and len(structured["results"]) == 1
        assert structured["manifest"]["status"] == "partial"
        from .test_package_revisions import prepared

        flow, aid = prepared(db, reviewed=True)
        exported = await client.call_tool(
            "export_submission_handoff",
            {
                "application_id": aid,
                "reviewed_hash": flow.review(aid)["hash"],
            },
        )
        assert not exported.is_error
        packet = exported.structured_content
        if "result" in packet:
            packet = packet["result"]
        assert packet["mode"] == "submit"
        assert "application.json" in packet["prompt"]
        assert not db.all("SELECT * FROM attempts")


def test_handoff_is_readable_and_requires_local_auth(db):
    from .test_workflow_tracker import ready

    flow, aid, path = ready(db)
    with TestClient(create_app(db.settings)) as client:
        headers = {"X-JSA-Token": db.settings.token()}
        response = client.post(
            "/api/applications/" + aid + "/handoff", json={}, headers=headers
        )
        assert response.status_code == 200
        url = response.json()["review_url"]
        assert client.get(url).status_code == 401
        result = client.get(url, headers=headers)
        assert result.status_code == 200 and "Saved answers" in result.text
        assert "120000" in result.text
        assert not db.all("SELECT * FROM attempts")
