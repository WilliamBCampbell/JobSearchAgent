"""The application and agent use the same versioned JobSpy MCP tools."""

import asyncio
import sys
from mcp import Client
from mcp.client.stdio import StdioServerParameters


class JobSpyClient:
    def __init__(self, db):
        self.db = db

    async def call(self, name, arguments):
        s = self.db.settings
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "jobsearch.mcp_server"],
            env={
                "JSA_DATABASE_URL": s.database,
                "JSA_STATE": str(s.auth_state or s.state),
                "JSA_PROFILE": s.profile_id,
                "JSA_WORKSPACE": str(s.workspace),
                "JSA_JOBSPY": str(s.jobspy),
                "JSA_ORIGIN": s.origin,
                "JSA_LIVE_SEARCH": "1" if s.live_search else "0",
                "JSA_JOBSPY_BACKEND": s.jobspy_backend,
                "JSA_JOBSPY_MCP_PYTHON": str(s.jobspy_mcp_python),
            },
        )
        async with asyncio.timeout(45):
            async with Client(params) as client:
                result = await client.call_tool(name, arguments)
                if result.is_error:
                    raise ValueError("JobSpy MCP tool failed: " + name)
                data = result.structured_content
                if isinstance(data, dict) and set(data) == {"result"}:
                    data = data["result"]
                if not isinstance(data, dict):
                    raise ValueError("JobSpy MCP returned an invalid tool result")
        self.db.audit("mcp.tool_completed", name, {"tool": name})
        return data
