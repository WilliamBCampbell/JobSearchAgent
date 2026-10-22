"""Bridge the official JobSpy MCP into durable, cancellable search jobs."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

VERSION = "1.4.0"
SDK_VERSION = "2.3.0"


def server_parameters(python):
    # Invoke the upstream entry point using its separate, pinned environment.
    # The SDK starts the child in another session. On Linux, tie its lifetime to
    # this worker so a hard timeout cannot leave the scraper running separately.
    code = f"""
import ctypes, os, signal, sys
from importlib.metadata import version
if sys.platform == 'linux':
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL) != 0:
        raise OSError('Could not bind JobSpy lifetime to its worker')
    if os.getppid() == 1:
        sys.exit(1)
if any(version(name) != expected for name, expected in {{'python-jobspy': '{VERSION}', 'mcp-jobspy': '{VERSION}', 'mcp': '{SDK_VERSION}'}}.items()):
    raise RuntimeError('Official JobSpy environment differs from the pinned versions')
from jobspy.mcp import main
main()
"""
    network_env = {
        key: os.environ[key]
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "REQUESTS_CA_BUNDLE",
            "SSL_CERT_FILE",
        )
        if key in os.environ
    }
    return StdioServerParameters(
        command=str(python), args=["-c", code], env=network_env
    )


async def search(python, parameters, output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Official JobSpy output must be a new file")
    arguments = {k: v for k, v in parameters.items() if k != "verbose"}
    arguments["save_to"] = str(output)
    async with Client(server_parameters(python)) as client:
        result = await client.call_tool("search_jobs", arguments)
    if result.is_error:
        message = "; ".join(c.text for c in result.content if hasattr(c, "text"))
        return {
            "jobs": [],
            "messages": [
                {
                    "level": "ERROR",
                    "message": message or "Official JobSpy search failed",
                }
            ],
            "upstream_errors": {parameters["site_name"][0]: message},
            "upstream_warnings": {},
        }
    data = result.structured_content
    if not isinstance(data, dict) or not isinstance(data.get("counts"), dict):
        raise ValueError("Official JobSpy returned an invalid search result")
    if data.get("saved") != str(output) or not output.is_file():
        raise ValueError("Official JobSpy did not save the complete search")
    rows = json.loads(output.read_text())
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Official JobSpy returned invalid full records")
    # Previews are capped at 25 per board; always ingest the full saved set.
    if len(rows) != sum(data["counts"].values()):
        raise ValueError("Official JobSpy's saved record count does not match")
    errors, warnings = data.get("errors") or {}, data.get("warnings") or {}
    messages = [
        {"level": "ERROR", "message": f"{board}: {message}"}
        for board, message in errors.items()
    ]
    return {
        "jobs": rows,
        "messages": messages,
        "upstream_errors": errors,
        "upstream_warnings": warnings,
        "search_id": data.get("search_id"),
    }


def main():
    # SearchService owns process-group deadlines and kills the MCP child as well.
    request = json.load(sys.stdin)
    result = asyncio.run(
        search(request["python"], request["parameters"], request["output"])
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
