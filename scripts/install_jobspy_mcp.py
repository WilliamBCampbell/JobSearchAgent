"""Install the pinned upstream MCP without changing the application's dependencies."""

import argparse
import asyncio
import json
import subprocess
import sys

from mcp import Client
from jobsearch.config import Settings, atomic_write, REPO
from jobsearch.official_jobspy import VERSION, server_parameters


async def verify(python):
    async with asyncio.timeout(30):
        async with Client(server_parameters(python)) as client:
            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            if not {"search_jobs", "get_job_details"} <= names:
                raise ValueError("The official JobSpy tools are missing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enable",
        action="store_true",
        help="Use the verified upstream MCP for live searches after services restart",
    )
    args = parser.parse_args()
    settings = Settings()
    installation = settings.auth_state or settings.state
    folder = installation / "integrations" / f"mcp-jobspy-{VERSION}"
    python = folder / "bin/python"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(folder)], check=True)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "-r",
            str(REPO / "requirements-jobspy-mcp.lock"),
        ],
        check=True,
    )
    subprocess.run([str(python), "-m", "pip", "check"], check=True)
    asyncio.run(verify(python))
    print(f"Verified official mcp-jobspy {VERSION}: search_jobs, get_job_details")
    if args.enable:
        atomic_write(
            installation / "jobspy-settings.json",
            json.dumps({"backend": "official_mcp"}),
        )
        print(
            "Official MCP selected. Restart the API and worker to apply it. Existing live-search opt-in still applies."
        )
    else:
        print("Installed for evaluation. The selected search backend has not changed.")


if __name__ == "__main__":
    main()
