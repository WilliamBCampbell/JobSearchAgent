"""Preparation and portable handoff tools. Browser submission is external."""

from typing import Any
from mcp.server import MCPServer
from .user_profiles import selected_settings
from .db import DB
from .search import SearchService, SearchRequest
from .scoring import duplicate_groups

mcp = MCPServer(
    "JobSpy Local",
    version="0.1.0",
    instructions="Search results are unverified discovery evidence. Read manifest warnings and verify official openings before recommendation. Submission is not available through these tools.",
)


def service():
    return SearchService(DB(selected_settings()))


@mcp.tool(structured_output=True)
def health() -> dict[str, Any]:
    """Check the established JobSpy environment and source capabilities."""
    return service().health()


@mcp.tool(structured_output=True)
def list_profiles() -> dict[str, Any]:
    """Read configured search profiles without contacting job boards."""
    return service().profiles()


@mcp.tool(structured_output=True)
def start_search(request: SearchRequest) -> dict[str, Any]:
    """Queue a durable search. A running local worker processes it; fixture mode is the default."""
    return service().start(request)


@mcp.tool(structured_output=True)
def search_progress(job_id: str) -> dict[str, Any]:
    """Read a durable job checkpoint, completion status, and errors."""
    return service().progress(job_id)


@mcp.tool(structured_output=True)
def search_results(job_id: str, offset: int = 0, limit: int = 20) -> dict[str, Any]:
    """Read a page of results plus the source-attributed manifest."""
    return service().results(job_id, offset, limit)


@mcp.tool(structured_output=True)
def cancel_search(job_id: str) -> dict[str, Any]:
    """Request cancellation; preserve completed raw queries and partial results."""
    return service().cancel(job_id)


@mcp.tool(structured_output=True)
def normalize_results(rows: list[dict], candidate: str = "fixture") -> dict[str, Any]:
    """Normalize cross-source discoveries and flag exact or ambiguous prior applications."""
    if candidate not in ("real", "fixture"):
        raise ValueError("Unknown candidate")
    db = DB(selected_settings())
    return {
        "groups": duplicate_groups(
            rows, db.all("SELECT * FROM applications WHERE candidate=%s", (candidate,))
        )
    }


@mcp.tool(structured_output=True)
def export_submission_handoff(
    application_id: str, reviewed_hash: str
) -> dict[str, Any]:
    """Snapshot this profile's files and context for Codex. Never upload or submit."""
    from .submission_handoff import SubmissionHandoff

    return SubmissionHandoff(DB(selected_settings())).export(
        application_id, reviewed_hash
    )


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
