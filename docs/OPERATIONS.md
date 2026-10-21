# Operations and recovery

## Local processes

`services.py` starts the API and worker separately. PID files and logs are in `$JSA_STATE/services`. The worker needs the same `JSA_DATABASE_URL`, `JSA_STATE`, `JSA_WORKSPACE`, and origin as the API. The default visible browser uses the desktop's `DISPLAY`; use `JSA_HEADLESS=1` only for automated tests.

Migrations are numbered SQL files in `migrations`. `jobsearch migrate` applies pending migrations under a PostgreSQL advisory lock. It never rewrites original career documents.

Long searches and ingestion expose checkpoints in the UI. Cancel preserves completed artifacts. Resume reuses completed source-query manifests or skips unchanged document hashes. Query workers have timeouts and bounded retries. API restart does not stop the separate worker. A crashed worker's lease expires after thirty seconds; the next worker recovers eligible jobs. A submission attempt is never blindly repeated.

## Approval-driven sending

The existing final-review button is the approval action. It queues one application, using the displayed answers and file versions. Greenhouse opens a fresh sending browser, uploads those files, submits, and checks for confirmation. The locally staged review window remains guarded; use **Approve & send this application** in the app. Browser edits do not update the saved answer plan.

To enable this capability, wait for active work to finish, then run:

```bash
.venv/bin/python scripts/services.py stop
.venv/bin/jobsearch submissions --mode enabled
.venv/bin/python scripts/services.py start
```

The setting is saved as `$JSA_STATE/submission-settings.json`, outside Git with mode 0600. Both services read it on startup. `jobsearch submissions` reports the saved startup configuration; `/health` reports the running API's mode. An explicit `JSA_LIVE_APPLICATIONS=0` or `1` overrides the saved setting and must agree across both services. The CLI refuses to change a saved setting while that environment override is present.

Enabling is blocked if existing real approvals, active submission states, or queued sends could resume. Resolve those records first. Enabling alone creates no approvals, jobs, or attempts. To disable sending, stop both services, run `jobsearch submissions --mode disabled`, and restart. After an interrupted attempt, reconcile employer receipt before trying anything else.

An upload failure can have a known outcome: the guarded browser finished its run without releasing the final application request. For that case, the application shows **Retry approved application**. Use that action to resume the original approved payload. Do not delete attempt records or force a failed submission job back into the queue. Crashes without a completed stop record and runs that released a POST still require receipt reconciliation.

Development tests still use synthetic fixtures. Production acceptance has not been tested by sending a real application. Unsupported controls and verification challenges need intervention. Tracker synchronization is a separate reviewed action. Enable it under **Metrics & tracker**; recording a receipt alone does not alter the ODS.

## Backup

Stop the local services before backing up files so the file snapshot agrees with the database dump:

```bash
.venv/bin/python scripts/services.py stop
.venv/bin/python scripts/services.py status
.venv/bin/python scripts/backup.py "$HOME/job-search-agent-backups"
.venv/bin/python scripts/services.py start
```

Wait for both services to report stopped. The archive contains a PostgreSQL custom-format dump and private state, including credentials and browser profiles. It is mode 0600; store it in a private or encrypted backup location. The original configured `JSA_WORKSPACE` corpus is not copied by this backup command. Back that workspace up separately using your existing document backup process.

## Restore without overwriting the current database

```bash
.venv/bin/python scripts/restore.py /path/to/backup.tar.gz /path/to/new-restore
export JSA_STATE=/path/to/new-restore/private-state
.venv/bin/python scripts/relocate_state.py /path/to/previous-private-state
.venv/bin/jobsearch migrate
.venv/bin/jobsearch ingest --candidate fixture
```

Restore creates a new database and state directory. It does not drop the current database. The relocation command updates current private artifact paths and recovery journals; historical audit events retain their original paths. Re-ingestion refreshes changed references. Do not run the restored and original workers against the same fixture browser port. Stop the original services before starting the restored copy.

After a restore, inspect unresolved submission attempts before resuming work. A database backup can describe an attempt that reached an employer after the backup time. Receipt reconciliation remains required.

## ODS conflicts and journals

The adapter initially treats imported ODS rows as authoritative. If a later import conflicts with database status, the UI shows both values. `POST /api/tracker/conflicts/{id}/resolve` accepts the ODS status without changing the source spreadsheet. New workflows can publish reviewed updates after tracker writes are enabled. Importing cannot grant a workflow a submission receipt.

Real and fixture synchronization require the current SHA-256 hash and a matching preview hash. It preserves ZIP members, checks unrelated rows, recalculates totals, saves a backup, and writes an atomic recovery journal. External edits block replacement. `jobsearch recover --candidate real` or `--candidate fixture` completes sidecar reconciliation when the tracker already matches the journal's new hash. If neither old nor new hash matches, recovery reports an external-edit conflict instead of replacing the file.

## Data boundaries

Application code is in the repository. Documents, embeddings, extracted text, browser profiles, tokens, submitted copies, source captures, reports, and logs are private state. Generated fixture packages use the existing Company/Role naming pattern under the private fixture workspace. Live search manifests use `_Research` under the configured real workspace only after live search is enabled.

Model endpoints receive nothing until their destination is explicitly enabled in the UI. Provider secrets must be supplied outside Git. The bundled Ollama contract has no API-key field; place authentication at a locally configured proxy if required. Tests use local employer fixtures. Greenhouse review blocks outbound candidate traffic; an approved sending run permits only the reviewed uploads and application request. Retrieved documents and employer pages supply evidence only; they cannot grant authorization or request arbitrary files.

## Codex worker recovery

Codex evaluation settings are separate from the embedding provider. Configure task routes under **Privacy & settings**; inspect runs under **Evaluations**. A failed model request records a safe error and retains the input packet for review. Resume retries a read-only evaluation with current evidence. Interrupted application submissions still require receipt reconciliation and cannot use this retry path.

The database backup includes inference runs, events, settings, and validated results. Private `llm-runs` directories contain schemas, not credentials or reasoning transcripts. Stop services before taking a consistent backup. Restoring a backup preserves its model enablement settings; inspect them before starting new work. The checked Codex CLI version and synthetic demonstration command are in [Codex jobs](CODEX_JOBS.md).

See [Metrics and tracker updates](TRACKER.md) for the page, enablement settings, preview contract, and rollback guidance.
