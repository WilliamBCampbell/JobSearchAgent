#!/usr/bin/env python3
"""Start or stop only this application's local API and worker."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from jobsearch.config import Settings, atomic_write, REPO

s = Settings()
folder = s.state / "services"
folder.mkdir(exist_ok=True)
action = sys.argv[1] if len(sys.argv) > 1 else "status"
selected = sys.argv[2] if len(sys.argv) > 2 else None
if selected not in (None, "api", "worker"):
    raise SystemExit("Choose api or worker, or omit the service to manage both")
for name, command in [
    ("api", [str(REPO / ".venv/bin/jobsearch"), "serve"]),
    ("worker", [str(REPO / ".venv/bin/jobsearch"), "worker"]),
]:
    if selected and selected != name:
        continue
    pidfile = folder / (name + ".pid")
    pid = int(pidfile.read_text()) if pidfile.exists() else None
    alive = bool(
        pid
        and Path(f"/proc/{pid}/cmdline").exists()
        and "jobsearch" in Path(f"/proc/{pid}/cmdline").read_text()
    )
    if action == "start" and not alive:
        with (folder / (name + ".log")).open("ab") as log:
            process = subprocess.Popen(
                command,
                cwd=REPO,
                stdout=log,
                stderr=log,
                start_new_session=True,
                env=os.environ,
            )
        atomic_write(pidfile, str(process.pid))
        if name == "api":
            import urllib.request

            for _ in range(100):
                try:
                    with urllib.request.urlopen(
                        s.origin + "/health", timeout=0.5
                    ) as response:
                        if response.status == 200:
                            break
                except Exception:
                    pass
                if process.poll() is not None:
                    raise SystemExit(
                        "API failed to start; inspect its private service log"
                    )
                time.sleep(0.1)
            else:
                raise SystemExit("API did not become ready")
        print(name + " started")
    elif action == "stop" and alive:
        os.kill(pid, signal.SIGTERM)
        for _ in range(150):
            cmd = Path(f"/proc/{pid}/cmdline")
            if not cmd.exists() or "jobsearch" not in cmd.read_text():
                break
            time.sleep(0.1)
        else:
            raise SystemExit(name + " is still stopping; wait before restarting it")
        print(name + " stopped")
    else:
        print(name + (": running" if alive else ": stopped"))
