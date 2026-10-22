"""Synthetic upstream protocol fixture; never contacts job boards."""

import json
import sys
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

server = MCPServer("synthetic-jobspy")


@server.tool(structured_output=True)
def search_jobs(
    search_term: str, site_name: list[str], save_to: str, is_remote: bool = True
) -> dict[str, object]:
    if search_term == "blocked":
        raise ToolError("indeed: response status code 429 (blocked: use a proxy)")
    output = Path(save_to)
    rows = [
        {
            "id": str(i),
            "site": site_name[0],
            "job_url": f"https://jobs.example/{i}",
            "title": "API Engineer",
            "company": "Example",
            "description": "Full job description",
        }
        for i in range(31)
    ]
    output.write_text(json.dumps(rows))
    return {
        "search_id": "synthetic",
        "counts": {site_name[0]: 30 if search_term == "bad-count" else 31},
        "saved": str(output),
        "jobs": rows[:2],
        "errors": {site_name[0]: "partial source failure"}
        if search_term == "partial"
        else {},
        "warnings": {site_name[0]: ["is_remote isn't supported, ignoring it"]}
        if site_name[0] == "linkedin"
        else {},
    }


if __name__ == "__main__":
    if "--worker" in sys.argv:
        request = json.load(sys.stdin)
        output = Path(request["output"])
        output.with_suffix(".request.json").write_text(json.dumps(request))
        print(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "fixture-result",
                            "site": "linkedin",
                            "title": "API Engineer",
                            "company": "Example",
                            "job_url": "https://jobs.example/1",
                            "description": "APIs",
                        }
                    ],
                    "messages": [],
                    "upstream_warnings": {
                        "linkedin": ["is_remote isn't supported, ignoring it"]
                    },
                    "upstream_errors": {},
                }
            )
        )
    else:
        server.run()
