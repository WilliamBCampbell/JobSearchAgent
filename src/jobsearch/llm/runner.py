"""Bounded Codex subprocess transport. Never persist reasoning or raw diagnostics."""

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
from ..config import atomic_write


class InferenceStopped(ValueError):
    pass


class InferenceFailure(ValueError):
    pass


def strict_schema(schema):
    if isinstance(schema, dict):
        schema = {k: strict_schema(v) for k, v in schema.items() if k != "default"}
        if schema.get("type") == "object":
            schema["additionalProperties"] = False
            schema["required"] = list(schema.get("properties", {}))
    elif isinstance(schema, list):
        schema = [strict_schema(x) for x in schema]
    return schema


def command(route, schema, output):
    return [
        "codex",
        "--no-daemon",
        "-a",
        "never",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--disable",
        "shell_tool",
        "--disable",
        "unified_exec",
        "--disable",
        "multi_agent",
        "--disable",
        "plugins",
        "--disable",
        "skill_search",
        "--disable",
        "shell_snapshot",
        "--enable",
        "skip_host_skill_discovery",
        "-c",
        'web_search="disabled"',
        "-c",
        "mcp_servers={}",
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        'model_provider="openai"',
        "-c",
        "model_reasoning_effort=" + json.dumps(route.effort),
        "-m",
        route.model,
        "--json",
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(output),
        "-",
    ]


def execute(
    prompt,
    schema,
    route,
    directory: Path,
    timeout,
    event,
    cancelled,
    command_factory=None,
):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    schema_path, output_path = directory / "schema.json", directory / "output.json"
    atomic_write(schema_path, json.dumps(strict_schema(schema)))
    output_path.unlink(missing_ok=True)
    args = (command_factory or command)(route, schema_path, output_path)
    # Pass account authentication through Codex's own store; never read or log tokens.
    env = {
        k: os.environ[k]
        for k in (
            "PATH",
            "HOME",
            "USER",
            "LANG",
            "CODEX_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
        )
        if k in os.environ
    }
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    args = [sys.executable, "-m", "jobsearch.llm.supervisor", str(os.getpid()), *args]
    p = subprocess.Popen(
        args,
        cwd=directory,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        umask=0o077,
    )
    start = time.monotonic()
    usage = {}
    failure = None
    selector = selectors.DefaultSelector()
    total_bytes = 0
    buffers = {p.stdout: b"", p.stderr: b""}

    def consume(line, stderr=False):
        nonlocal failure, usage
        if stderr:
            # Diagnostics can contain prompts, paths, tokens, or model text. Keep only known classes.
            low = line.lower()
            if b"rate limit" in low or b"usage limit" in low:
                failure = "Codex usage limit reached"
            return
        try:
            data = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            raise InferenceFailure("Invalid Codex event stream")
        kind = data.get("type", "")
        if kind in ("turn.failed", "error"):
            failure = (
                failure
                or "Codex returned an error; check account access or model availability"
            )
            event("provider_error", {})
        elif kind == "turn.completed":
            usage = {
                k: v
                for k, v in data.get("usage", {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
            event("turn_completed", {"usage": usage})
        elif kind in ("thread.started", "turn.started"):
            event(kind.replace(".", "_"), {})
        elif kind.startswith("item."):
            item = data.get("item", {})
            itype = item.get("type")
            if itype in (
                "command_execution",
                "mcp_tool_call",
                "file_change",
                "web_search",
            ):
                raise InferenceFailure(
                    "Inference attempted a tool action; result rejected"
                )
            if itype == "agent_message" and kind == "item.completed":
                event("response_received", {})
            # Reasoning is intentionally neither displayed nor persisted.

    try:
        event("process_started", {"pid": p.pid})
        pending_input = memoryview(prompt.encode())
        os.set_blocking(p.stdin.fileno(), False)
        selector.register(p.stdin, selectors.EVENT_WRITE)
        for stream in buffers:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        last_heartbeat = time.monotonic()
        while selector.get_map():
            if time.monotonic() - last_heartbeat >= 5:
                event("waiting_for_response", {})
                last_heartbeat = time.monotonic()
            if cancelled():
                raise InferenceStopped("Inference cancelled")
            if time.monotonic() - start > timeout:
                raise InferenceFailure("Codex evaluation timed out")
            for key, _ in selector.select(timeout=0.25):
                stream = key.fileobj
                if stream is p.stdin:
                    try:
                        written = os.write(stream.fileno(), pending_input[:65536])
                        pending_input = pending_input[written:]
                    except BrokenPipeError:
                        pending_input = memoryview(b"")
                    if not pending_input:
                        selector.unregister(stream)
                        stream.close()
                    continue
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    if buffers[stream]:
                        consume(buffers[stream], stream is p.stderr)
                    selector.unregister(stream)
                    continue
                total_bytes += len(chunk)
                if total_bytes > 2_000_000:
                    raise InferenceFailure("Codex output limit exceeded")
                buffers[stream] += chunk
                while b"\n" in buffers[stream]:
                    line, buffers[stream] = buffers[stream].split(b"\n", 1)
                    if line.strip():
                        consume(line, stream is p.stderr)
        remaining = max(0.1, timeout - (time.monotonic() - start))
        try:
            code = p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise InferenceFailure("Codex evaluation timed out") from None
        if cancelled():
            raise InferenceStopped("Inference cancelled")
        if code != 0 or failure:
            raise InferenceFailure(failure or "Codex process failed")
        if not output_path.exists() or output_path.stat().st_size > 150_000:
            raise InferenceFailure("Missing or oversized Codex result")
        try:
            output = json.loads(output_path.read_text())
        except (ValueError, UnicodeDecodeError):
            raise InferenceFailure("Codex did not return valid JSON") from None
        return output, usage
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=2)
        selector.close()
        for stream in (p.stdin, p.stdout, p.stderr):
            stream.close()
        output_path.unlink(missing_ok=True)
