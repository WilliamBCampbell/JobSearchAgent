# Metrics and tracker updates

Open **Metrics & tracker** from the sidebar. The page reads the current ODS file on entry and when you select **Refresh metrics**. It shows spreadsheet totals, status and monthly application charts, searchable records, current app workflows, and memory/search measurements. The overview and activity pages no longer repeat those summary metrics.

The spreadsheet and app counts describe different records. Imported rows are excluded from the app workflow chart, but a new workflow can also have a spreadsheet row. Do not add the two totals. Submitted includes active applications and rejections; offers are included in active. The monthly chart uses recorded application dates and reports missing or unreadable dates. Memory measurements are cumulative across real and fixture workspaces; answer latency is averaged over recorded lookup requests.

## Review and save

New installations keep real tracker writes disabled. **Enable writes to this tracker** enables the displayed workspace without queuing work. Each update still needs a preview and an explicit **Save reviewed tracker update** action. The receipt panel also offers **Preview tracker update**.

1. Choose an application under **Tracker updates**.
2. Load its preview. Inspect the current and proposed row, including status, dates, documents, and notes.
3. Save the reviewed update. The server checks the application revision, receipt IDs, current spreadsheet hash, and generated-file hashes again.

Matching uses normalized employer names and exact requisitions. Recognized Greenhouse application URLs supply the public posting ID alongside the internal requisition. Distinct requisitions remain separate. Ambiguous identities stop the update. A submission already recorded in the tracker returns **Already recorded**, preserving its original dates, documents, and notes.

A submitted workflow requires a receipt. Only documents recorded as actually attached in its frozen submission payload are listed as attachments. Manual receipt records without attachment evidence use **Not recorded**. Importing a spreadsheet row cannot grant a workflow a receipt or reset its submission history.

## Files and recovery

The shared real/fixture writer locks `.tracker.lock`, saves a backup under `_Backups`, verifies the archive and unrelated rows, and atomically replaces the ODS. It updates summary totals and writes generated application notes, a checklist, and a handoff under `_Agent`. The original top-level handoff and other career documents remain unchanged. Unknown summary fields, including an existing manual-follow-up count, are preserved.

A journal under `_Recovery` records the old/new hashes and the generated files needed to finish an interrupted save. **Check pending tracker recovery** finishes those files only when their hashes still match. External edits block recovery. If replacement never happened, the journal is marked aborted and a fresh preview is required.

To recover from the command line, select the workspace explicitly:

```bash
.venv/bin/jobsearch recover --candidate real
.venv/bin/jobsearch recover --candidate fixture
```

For an intentional rollback, stop the API, retain a copy of the current ODS and recovery journal, and inspect the journal's backup. Restore that ODS and any generated-file backups as one reviewed operation, then import the restored spreadsheet. Do not restore over external edits. A rollback does not erase application receipts, approvals, or audit history. Full database/private-state backup instructions are in [Operations](OPERATIONS.md).

## Configuration and API

The UI saves `$JSA_STATE/tracker-settings.json` privately and applies the mode immediately to the running API. The worker never queues tracker writes. For a startup setting:

```bash
.venv/bin/jobsearch tracker-writes --mode enabled
.venv/bin/jobsearch tracker-writes --mode disabled
.venv/bin/jobsearch tracker-writes --mode status
```

Restart only the API after CLI changes with `scripts/services.py stop api` followed by `scripts/services.py start api`, using `.venv/bin/python`. This preserves the worker and any open employer browser. `JSA_LIVE_TRACKER_WRITES=0` or `1` overrides the saved mode; the UI and CLI refuse to change it while that override is present. Application sending has a separate setting and authorization path.

Authenticated endpoints:

- `GET /api/tracker/metrics?candidate=real|fixture`: fresh spreadsheet, charts, workflow counts, and measurements.
- `PUT /api/settings/tracker-writes`: strict boolean `enabled`.
- `GET /api/tracker/preview/{application_id}`: read-only row preview with `expected_hash` and `review_hash`.
- `POST /api/tracker/update/{application_id}`: requires `expected_hash` and `reviewed_hash` from that preview.
- `POST /api/tracker/recover?candidate=real|fixture`: finish pending generated records.

The writer supports the existing eight-column application layout. Repeated populated rows, merged application cells, multiple possible destination sheets, and ambiguous identity matches require manual reconciliation. Graphs describe recorded outcomes; they do not predict hiring probability.
