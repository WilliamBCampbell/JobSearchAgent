# Publication checks

The public repository starts from a sanitized source snapshot. Private development history is not published. Runtime configuration, personal documents, original trackers, database contents, credentials, browser profiles, and development evidence belong outside Git.

## Local configuration

Copy `.env.example` to ignored `.env.local`, edit it, and load it explicitly:

```bash
chmod 600 .env.local
set -a
. ./.env.local
set +a
```

The application does not automatically load dotenv files. Bootstrap writes randomly generated database credentials to `JSA_STATE/database-url`; the API token and saved settings also live under private state. Existing runtime state is preserved when publishing source. The public example contains no credentials.

## Check before committing

Install [Gitleaks](https://github.com/gitleaks/gitleaks) and enable the repository hook:

```bash
git config core.hooksPath .githooks
# Optional when Gitleaks is not on PATH:
git config jobsearch.gitleaks /path/to/gitleaks
git add <source-files>
python3 scripts/check_publication.py
gitleaks git --pre-commit --staged --redact --no-banner .
```

The Python check reads the staged Git blobs, not unstaged working files. It rejects private/generated file paths, binary files, personal home paths, non-fixture contact data, and private denylist matches. An optional ignored `.privacy-denylist.json` accepts `{"literals": ["private value"]}` for names, addresses, account identifiers, or other strings specific to a local workspace. `JSA_PRIVACY_DENYLIST` may point to a private file outside the repository. Findings identify a rule and location without printing the matched value.

Before pushing, inspect history as well:

```bash
python3 scripts/check_publication.py --history
gitleaks git --log-opts="--all" --redact --no-banner .
git diff --cached --stat
```

GitHub Actions runs both history checks with read-only permissions. The Gitleaks binary is version-pinned and checksum-verified. Its configuration retains default rules with one narrow exception for a literal Greenhouse field name that resembles a LinkedIn credential; no files or commits are excluded wholesale.

Ignore rules do not remove previously committed files. If a secret or personal document enters history, stop publication and inspect all refs, tags, and exported artifacts. Rotate exposed credentials. A backup containing the original history is itself private and must not be uploaded.

## Limits

These checks complement manual review. They cannot prove the absence of all personal information, infer every sensitive detail from prose, or validate every external dependency. Public examples use synthetic candidate identities and reserved example domains. External JobSpy adapters and writing-skill installations must be reviewed separately before redistribution.
