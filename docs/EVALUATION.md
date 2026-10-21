# Evaluation coverage

Automated evaluation uses synthetic candidates, isolated test databases, local employer forms, and offline model responses. Real employer acceptance and live model quality are separate from fixture correctness. Personal development reports, source captures, screenshots, application records, and employer-specific session history are not published.

| Area | Tests |
| --- | --- |
| Memory and retrieval | Confirmed-memory reuse, corrections and cache invalidation, scoped answers, later-page evidence, generated-draft authority, duplicate formats |
| Search and fit | MCP protocol calls, partial failures, ignored filters, duplicate applications, mandatory qualification gates, separate rubrics, remote/pay preferences |
| Document packages | Evidence references, employment history, rendering and text checks, version-bound revisions, immutable files, document review |
| Application workflow | Sequential selection through fixture receipt, saved checkpoints, missing answers, explicit approval, cancellation and resumption |
| Browser transport | Reviewed fields and upload contracts, verification pauses, bounded continuation, missing receipts, restart protection, guarded retries |
| Tracker | Reviewed updates, external edits, unrelated rows, totals, separate requisitions, deferred records, recovery journals |
| User interface | Guided next steps, corrections, requirement decisions, package feedback, answer scopes, narrative notes separated from employer choices |

Publication preparation passed 23 focused tests in `test_publication.py`, `test_documents.py`, and `test_writing_instructions.py`. These cover staged-file and historical privacy checks, portable configuration paths, document rendering, evidence validation, and writing-instruction delivery. Ruff passed for the changed Python files. The sequential fixture journey and recovery suites are retained in `tests/`; the full application suite was not rerun for publication. These results do not establish that every live ATS or model response has been validated.

## Run locally

After installing dependencies and starting the local database:

```bash
.venv/bin/pytest -q
npm --prefix web run build
```

Browser tests use port 8766 and must not run in competing processes. The live application uses port 8765 by default. Tests create and remove their own databases; they do not use real candidates for submissions. Several tests render actual documents and require Chromium, LibreOffice, and Poppler.

Publication validation and its limitations are documented in [publication checks](PUBLICATION.md).
