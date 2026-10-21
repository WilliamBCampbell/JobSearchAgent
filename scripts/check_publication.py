#!/usr/bin/env python3
"""Check the Git index (and optionally history) without printing private values."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def git(*args):
    return subprocess.check_output(["git", *args])


EMAIL = re.compile(r"[A-Za-z0-9_.+%-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
HOME = re.compile(r"/(?:home|Users)/[A-Za-z0-9_.-]+")
PROFILE = re.compile(r"linkedin\.com/in/([A-Za-z0-9_-]+)", re.I)
PHONE = re.compile(
    r"(?<!\w)(?:\+1[ .-]?)?\(?([2-9]\d{2})\)?[ .-]([2-9]\d{2})[ .-](\d{4})(?!\w)|(?<!\w)\+1([2-9]\d{2})([2-9]\d{2})(\d{4})(?!\w)"
)
PRIVATE_PATH = re.compile(
    r"(^|/)(?:\.env(?:\..*)?|\.privacy-denylist\.json|(?:api|access)-token|database-url|"
    r"credentials|secrets|\.secrets|profiles|state|private-state|job_search|workspace|"
    r"backups|reports|llm-runs|_Research|_Agent|\.venv|node_modules)(/|$)|"
    r"\.(?:pdf|docx?|ods|sqlite\w*|db|png|jpe?g|zip|dump|bundle|pem|key|p12|pfx|har|mbox|eml|pst|msg|tar|tgz|7z)$|"
    r"(^|/)(?:Application_Answers|Confirmed_Application_Answers|Submission_Receipt|"
    r"Document_Content|Document_Review|Package_Manifest)\.json$|"
    r"(^|/)config\.local\.|\.local\.(?:json|toml|ya?ml)$",
    re.I,
)


def findings(text, literals):
    found = set()
    for number, line in enumerate(text.splitlines(), 1):
        if HOME.search(line):
            found.add(("personal-home-path", number))
        for match in EMAIL.finditer(line):
            domain = match[1].lower()
            if not (
                domain.endswith(
                    (".invalid", ".test", ".example", ".users.noreply.github.com")
                )
                or domain
                in {
                    "example.com",
                    "example.org",
                    "example.net",
                    "users.noreply.github.com",
                }
            ):
                found.add(("non-fixture-email", number))
        for match in PROFILE.finditer(line):
            if match[1] not in {"example", "fixture-candidate", "corrected"}:
                found.add(("personal-profile-url", number))
        for match in PHONE.finditer(line):
            groups = match.groups()
            exchange = groups[1] or groups[4]
            if exchange != "555":
                found.add(("non-fixture-phone", number))
        if any(
            re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", line, re.I)
            for value in literals
        ):
            found.add(("private-denylist-match", number))
    return sorted(found)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    root = Path(git("rev-parse", "--show-toplevel").decode().strip())
    os.chdir(root)
    deny = Path(os.getenv("JSA_PRIVACY_DENYLIST", root / ".privacy-denylist.json"))
    literals = json.loads(deny.read_text()).get("literals", []) if deny.exists() else []
    if any(not isinstance(v, str) or len(v) < 3 for v in literals):
        raise SystemExit(
            "Private denylist entries must be strings of at least three characters"
        )
    blobs = set()
    errors = []

    def entry(mode, oid, path, origin):
        if mode != "100644" and mode != "100755":
            errors.append((origin, path, "non-regular-file", 0))
        if path != ".env.example" and PRIVATE_PATH.search(path):
            errors.append((origin, path, "private-or-generated-file", 0))
        pair = (oid, path)
        if pair in blobs:
            return
        blobs.add(pair)
        try:
            data = git("cat-file", "blob", oid).decode("utf-8")
            if "\0" in data:
                raise UnicodeError()
        except UnicodeError:
            errors.append((origin, path, "binary-content", 0))
            return
        errors.extend(
            (origin, path, rule, line) for rule, line in findings(data, literals)
        )

    for raw in git("ls-files", "--stage", "-z").split(b"\0"):
        if raw:
            meta, path = raw.decode().split("\t", 1)
            mode, oid, stage = meta.split()
            if stage != "0":
                errors.append(("index", path, "unmerged-file", 0))
            entry(mode, oid, path, "index")
    if args.history:
        for commit in git("rev-list", "--all").decode().splitlines():
            origin = commit[:12]
            metadata = git(
                "show", "-s", "--format=%an <%ae>%n%cn <%ce>%n%B", commit
            ).decode()
            errors.extend(
                (origin, "<commit metadata>", rule, line)
                for rule, line in findings(metadata, literals)
            )
            for raw in git("ls-tree", "-r", "-z", commit).split(b"\0"):
                if raw:
                    meta, path = raw.decode().split("\t", 1)
                    mode, kind, oid = meta.split()
                    if kind != "blob":
                        errors.append((origin, path, "external-git-object", 0))
                    else:
                        entry(mode, oid, path, origin)
    for origin, path, rule, line in errors:
        print(f"{origin} {path}:{line}: {rule}")
    if errors:
        print(f"Publication check blocked: {len(errors)} findings. Values are omitted.")
        return 1
    print(
        f"Publication check passed: {len(blobs)} file versions scanned. This check supplements secret scanning and manual review."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
