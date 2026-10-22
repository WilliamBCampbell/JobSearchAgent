# Job Search Agent

A local job-search preparation application with user profiles, a React UI, PostgreSQL retrieval, JobSpy search tools, and versioned resumes, cover letters, and answers. Submission happens in a separate Codex CLI conversation using an exported prompt and private packet with absolute document paths and application context. The app does not upload or submit applications.

The prior browser-submission implementation is archived in [JobSearchAgentSubmission](https://github.com/WilliamBCampbell/JobSearchAgentSubmission). Read the [migration plan](docs/EXTERNAL_SUBMISSION_PLAN.md) for the current boundary and follow-up work.

After reviewing an application, select **Create submission prompt** and **Copy submission prompt**. You can also export with `jobsearch handoff --profile PROFILE_ID --application APPLICATION_ID`. Paste the prompt into Codex CLI using your configured Playwright or Puppeteer tools. Return to the app to record employer receipt and review tracker updates. Exporting alone never marks an application submitted.

## Start

Requirements: Linux, Python 3.13, Node 20.19 or later, Docker, Chromium at `/usr/bin/chromium`, LibreOffice, and Poppler. Live discovery can use the official `mcp-jobspy` server in a separate pinned environment. Run `.venv/bin/python scripts/install_jobspy_mcp.py --enable` after bootstrap, then restart the services. Offline fixture searches need no JobSpy checkout. The retained legacy backend requires the existing adapter checkout selected by `JSA_JOBSPY`. Package generation also requires the separately installed no-ai-slop skill described in [workspace integrations](docs/INVENTORY.md).

```bash
git clone https://github.com/WilliamBCampbell/JobSearchAgent.git job-search-agent
cd job-search-agent
cp .env.example .env.local
# Edit .env.local for your machine. Keep it private.
set -a
. ./.env.local
set +a
python3 scripts/bootstrap.py
.venv/bin/python scripts/services.py start
.venv/bin/jobsearch open
```

The UI is at **http://127.0.0.1:8765**. `jobsearch open` supplies the local access token through a URL fragment, removes it from browser history after unlocking, and establishes an HttpOnly local session. If opening the browser is unavailable, run `.venv/bin/jobsearch token` and paste the value into the unlock screen. Do not share that token.

```bash
.venv/bin/python scripts/services.py status
.venv/bin/python scripts/services.py stop
```

The PostgreSQL container is `job-search-agent-db`; its volume is `job-search-agent-pgdata`. Database credentials, API token, browser profiles, embeddings, audit state, and artifacts stay outside Git under `~/.local/share/job-search-agent`. Bootstrap creates random database credentials in a private file. The database binds only to localhost.

## Find roles online

Open **Find roles**, select a track, and click **Find my best matches**. Leave the run working while the agent searches, verifies, evaluates, and prepares the shortlist. Review the resulting packages and deductions. Pause, resume, cancellation, and previous runs are available on the same screen. [Autonomous search](docs/AUTONOMOUS_SEARCH.md) documents the search bounds. [Employer forms](docs/EMPLOYER_FORMS.md) describes automatic answer planning and the current browser adapter coverage.

Enable online sources with `JSA_LIVE_SEARCH=1` when starting the services, and enable the displayed Codex destination in Model settings. Sample searches remain under **Sample search for testing**.

The [application workflow guide](docs/WORKFLOW.md) explains each step, its gates, and how saved work resumes after a correction or interruption.

## User profiles

Use **User profile** in the sidebar to switch searches. Open **Privacy & settings → Create user profile** to add one for yourself or a household member. Enter a name and an existing source document folder. Folders must be separate, with no parent/child overlap between profiles. Open **Documents** in the new profile to import records or index files.

After updating an existing installation, restart both the API and worker. Startup applies the migration. `JSA_WORKSPACE` supplies the default profile's source folder when it is first registered; later runs use the saved profile folder.

Existing records stay in **My profile**. Each additional profile starts with empty records and separate answers, applications, searches, model settings, generated files, and browser sessions. Switching profiles reloads the view and clears unsaved drafts. Other tabs and queued jobs retain their profile. **Data mode** under **Privacy & settings** selects real records or synthetic demonstration data within that profile. Real records are the default.

Profiles share this installation's local access token. Anyone with that token can manage all profiles; profiles are not separate login accounts. The worker processes all profiles automatically. CLI operations accept `--profile <id>`, and external MCP servers select one with `JSA_PROFILE=<id>` while keeping `JSA_STATE` pointed at the installation's state directory. Profile IDs are available in the page URL and `GET /api/profiles`. The MCP `list_profiles` tool still lists JobSpy search presets.

In-app submission is disabled for every profile. Tracker writes have a separate deployment setting. Model evidence transfer must be enabled separately in each profile. [Profile storage and routing](docs/ARCHITECTURE.md#user-profile-isolation) describes the database migration and request boundary.

## Demonstration

Open **Privacy & settings** and choose **Fixture demonstration** under **Data mode**:

1. Load the demonstration, then open **Find roles** and search the fixture sources. The manifest shows partial LinkedIn results, an ignored remote filter, and a prior application duplicate.
2. Choose **Verify & inspect fit** for Northstar Demo. Review its requirement matrix, compensation components, and Ohio evidence. Harbor Demo remains conditional despite its high numeric score because a central production-tenure requirement is unmet.
3. Select the role and prepare the package. Preview the documents, request changes if needed, then choose **Documents look good**. Select the actual attachments; a prepared cover letter is not attached automatically.
4. Choose **Continue to application form**. Confirm the missing salary and travel answers for this application. The worker resumes after the last answer.
5. Inspect the final review and click **Approve & submit fixture application**. The worker records an attempt before the click and captures a local receipt before marking the application submitted.
6. Sync the fixture tracker. Its unrelated rows, separate requisitions, and deferred record remain intact. View the audit trail under Activity and charts under Metrics & tracker.

`.venv/bin/python scripts/demo.py` runs the same fixture flow through the authenticated API and saves a private demonstration report. Its approval is explicitly limited to the synthetic local employer. No real application is submitted.

`.venv/bin/python scripts/demo_portal.py` demonstrates the employer-form adapter through the running UI, using only a synthetic employer and an offline model. It saves review and receipt screenshots under private state.

For read-only inspection, choose **Current profile records**, import the tracker and confirmation records, and inspect **Profile & answers**. Full ingestion is available under **Documents**. File hashes, references, extraction warnings, conflicts, and private intake reports remain available for review.

## Commands

```bash
.venv/bin/jobsearch inventory --candidate real
.venv/bin/jobsearch import --candidate real
.venv/bin/jobsearch ingest --candidate real
.venv/bin/jobsearch seed
.venv/bin/jobsearch recover
.venv/bin/pytest -q
npm --prefix web run build
```

Environment variables are described in `.env.example`. The application reads environment variables directly; it does not load `.env` automatically. Keep actual configuration outside the repository. `JSA_LIVE_SEARCH=1` enables read-only live board searches through the selected discovery backend. The UI defaults to the existing workspace and presents the autonomous real-search action. MCP and the API use the same search contracts. Raw live search results go under the real workspace's `_Research`; fixture results go under the private fixture workspace.

## JobSpy MCP

The application MCP retains durable search and workflow tools. The official upstream `mcp-jobspy` server supplies live discovery when enabled. These are separate servers with different responsibilities. Read the [comparison, live results, installation, and rollback guide](docs/JOBSPY_MCP_COMPARISON.md).

Start the API worker before requesting searches through MCP. Add this stdio server to an MCP client configuration, adjusting the absolute paths if needed:

```json
{
  "mcpServers": {
    "jobspy-local": {
      "command": "/path/to/job-search-agent/.venv/bin/jobspy-mcp",
      "env": {
        "JSA_STATE": "/path/to/private-state",
        "JSA_JOBSPY": "/path/to/JobSpy",
        "JSA_LIVE_SEARCH": "0"
      }
    }
  }
}
```

Tools: `health`, `list_profiles`, `start_search`, `search_progress`, `search_results`, `cancel_search`, `normalize_results`, and `export_submission_handoff`. The exporter returns a private packet path and a copyable prompt with prepared fields and answers. They use the same service contracts as the API. No submission or outreach tool is exposed.

## Models and retrieval

Inference and document retrieval are configured separately. The **Evaluations** screen queues typed jobs through `codex exec`, displays operational events and usage, and presents evidence-linked results for review. Default routes use Luna for extraction and selection, Sol for fit assessment and drafting, and Astra for conflict review. Each route is editable in **Privacy & settings**. A fresh installation uses offline fixtures until Codex evidence transfer is enabled.

The default deployment uses local lexical search. Codex can suggest alternate search terms and interpret retrieved passages; this does not create a learned semantic index. Optional Ollama embeddings remain available, and the deterministic fixture vectors remain test surrogates. Changing an embedding model requires re-ingestion; changing an evaluation model does not.

Package preparation runs separate structured writing and claim-review tasks through the inference service. The renderer follows the workspace's HTML → LibreOffice DOCX → US Letter PDF process and produces actual PDF page previews. Titles and dates come directly from confirmed employment records; every written claim retains its evidence references. Generated documents remain drafts until reviewed. See [document preparation](docs/DOCUMENTS.md) for quality checks and [Codex jobs and model routing](docs/CODEX_JOBS.md) for privacy and recovery.

## Implemented controls

| Capability | Implementation |
| --- | --- |
| Workspace import | ODS authority, original hashes, confirmation records, scoped answers, employment corrections, deferral records, manual queue |
| Retrieval | Versioned chunks, lexical and vector fusion, section/page/table references, neighboring and full-document expansion, rendering groups, extraction warnings |
| Memory | Typed PostgreSQL facts, explicit confirmation, scoped commitments, correction and conflict flows, evidence-dependent caches and metrics |
| Search | Official JobSpy MCP bridge or legacy worker, durable jobs, bounded queries, cancellation, full saved results, source warnings, duplicate checks |
| Inference | Typed Codex jobs, task-specific model routes, monitored JSON output, cancellation, restart recovery, evidence-dependent caching |
| Fit | Separate engineering and solutions rubrics, mandatory gaps, minima, Ohio and compensation caps, maximum-five shortlist function |
| Requirement review | Confirm or deny central requirements with scoped notes; rerun fit scoring through the configured model |
| Packages | HTML, DOCX, PDF, answer records, review page, evidence IDs, immutable submitted copies, actual-attachment tracking |
| Workflow | Durable states, form inspection, profile-based answers, copy controls, private Codex handoff packets, pause/resume, receipt review |
| Intervention inbox | Persistent notices, manual handoffs for uncertain required answers, acknowledgment without clearing blockers, scoped confirmations, automatic resumption |
| Records | Read-only real imports, conflict resolution, fixture ODS updates, backups, atomic replacement, journals, notes/checklist/handoff, manual reports |
| Privacy | Localhost services, token authentication, origin checks, isolated browser profiles, artifact path restrictions, no hosted transfer by default |

Open **Needs attention** for applications waiting on an answer, browser verification, manual follow-up, receipt check, or approval. [Manual answers and notifications](docs/INTERVENTIONS.md) describes the handoff and acknowledgment behavior.

In **Fit assessment**, use **Confirm** or **Deny** for central requirements, add an optional note, then choose **Rescore job** to rerun the evaluation. See [Requirement decisions and rescoring](docs/REQUIREMENT_REVIEW.md).

## Limits

The app reads Lever, Greenhouse, and Breezy application forms and prepares answers, including profile-based AI suggestions when needed. Each question and answer can be copied separately or together. The submission prompt includes those fields and answers plus absolute paths to the selected documents. Unsupported forms need inspection in the external browser session. Codex CLI handles form entry, uploads, and submission; CAPTCHA and employer verification can still require your intervention. Legacy sending settings cannot enable the retired in-app senders. Record actual employer confirmation afterward. Real tracker writes retain their separate reviewed preview and deployment setting. [Tracker controls](docs/TRACKER.md) describe backups and recovery.

Official verification requires an allowed employer host, an identifiable requisition, an open page, and an application link. Redirects and ambiguous pages need review. Search fixtures do not prove current real openings. Live JobSpy searches and read-only employer verification have been demonstrated; discovery alone does not establish that an opening is current.

Scanned PDFs report extraction failures; OCR is not bundled. EML and MBOX plain-text bodies are supported; proprietary Outlook MSG/PST exports and HTML-only email need conversion. DOCX pagination is a hint until rendered to PDF. Automatic rendering groups use the shared basename; differently named copies need explicit grouping.

The current reranker uses term overlap, not a cross-encoder. A learned Ollama model has not been exercised. The coordinator applies validated Codex requirement matrices through the code-enforced scoring gates. Successful synthetic evaluations do not establish accuracy across arbitrary postings.

The tracker adapter covers the inspected eight-column Applications layout. Other sheet layouts require a mapping. Real document and status conflicts are shown for review instead of being silently resolved. Generated sidecars are managed under `_Agent` in the fixture workspace; the old real handoff and checklists remain untouched.

See [workspace inventory](docs/INVENTORY.md), [architecture decisions](docs/ARCHITECTURE.md), [operations and recovery](docs/OPERATIONS.md), and [evaluation results](docs/EVALUATION.md). Repository skills live under `.agents/skills`.

## Public source and private configuration

This repository contains source code and synthetic fixtures. Career documents, runtime state, credentials, browser profiles, reports, and local configuration must stay outside Git. See [publication checks](docs/PUBLICATION.md) for the staged-file and full-history scans, and [security reporting](SECURITY.md) before sharing logs or screenshots. The existing rubric uses an Ohio-oriented location key; this is a policy limitation, not a portable inference about every candidate.
