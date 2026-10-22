# Autonomous search and preparation

Open **Find roles**, enter a role or keywords, and choose **Find my best matches** (or press Enter). The app searches real Indeed and LinkedIn listings through the local JobSpy MCP server. The worker then checks official openings, compares prior applications, extracts requirements, evaluates fit through Codex, ranks qualifying roles, and prepares up to five packages. The screen survives reloads and shows saved progress, source warnings, exclusions, and package previews.

The default run examines up to 60 discovery groups and evaluates up to 12 verified openings. These bounds and the primary limit are typed fields in `HuntRequest`. The role text becomes one search term per board, with surrounding whitespace removed and punctuation preserved. Blank input is rejected; the limit is 300 characters. The saved request, progress display, and search history retain that role. Older requests without a role continue to use their track's preset queries.

**Evaluation settings** selects the fit assessment track independently of the search text. Software engineering, applied AI, and solutions engineering remain the available assessment tracks; solutions engineering retains its separate rubric. Custom roles do not create new rubrics or remove the existing fit gates. Results beyond the configured bounds remain in the saved search results. No batch is padded to reach five.

## Additional JobSpy search controls

The app runs its own `jobspy-mcp` server in `src/jobsearch/mcp_server.py`. Its `start_search` tool accepts `SearchRequest` from `src/jobsearch/search.py`. The separate [upstream MCP server](https://github.com/speedyapply/JobSpy/blob/main/mcp/README.md) exposes `search_jobs` with JobSpy search options. The table below describes the app's local integration, checked against the local JobSpy checkout at `1fffbcd4c1d822740157459fe527a916d3ce7a43` and the [upstream parameter reference](https://github.com/speedyapply/JobSpy#parameters-for-scrape_jobs) on October 21, 2026.

| Possible user control | Parameter | Local integration and limits |
| --- | --- | --- |
| Job boards | `sites` / JobSpy `site_name` | MCP already accepts Indeed, LinkedIn, Glassdoor, ZipRecruiter, Bayt, Naukri, BDJobs, and HelloWork. The hunt currently uses Indeed and LinkedIn. |
| Posting age | `hours` / `hours_old` | MCP accepts 1 to 8,760 hours; default 168. Glassdoor rounds to whole days. |
| Remote only | `remote` / `is_remote` | MCP already accepts this; default true. The local LinkedIn scraper ignores it. False removes the remote-only request; it does not mean onsite only. |
| Results per query per board | `results` / `results_wanted` | MCP accepts 1 to 100, default 15. Hunts use `results_per_query`, bounded to 5 to 50, default 20. This controls retrieval volume. |
| Multiple role queries | `terms` | MCP accepts up to six terms. The new role field submits one term. |
| City, state, or region | `location` | JobSpy and the local runner support it. `SearchRequest` needs a field and forwarding; the service currently inherits the saved profile's location. |
| Search radius | `distance` | JobSpy accepts miles, default 50. Needs MCP and adapter wiring. Board support varies. |
| Country | `country_indeed` | JobSpy supports country selection for Indeed and Glassdoor. Needs MCP and adapter wiring; other boards use location or have regional coverage. |
| Employment type | `job_type` | JobSpy supports types such as full-time, part-time, contract, temporary, and internship. Needs MCP and adapter wiring. The local LinkedIn scraper ignores it; other boards support different subsets. |
| Easy Apply | `easy_apply` | JobSpy supports a filter for applications hosted on a job board. Needs MCP and adapter wiring. It does not grant permission to apply. |
| Specific LinkedIn employers | `linkedin_company_ids` | JobSpy accepts numeric company IDs. Needs MCP and adapter wiring, plus a way for users to supply IDs or resolve employer names. |
| More search results | `offset` | JobSpy accepts a source search offset. Needs MCP and adapter wiring. Existing `search_results(offset, limit)` only pages through already saved results. |

Start with location, posting age, board selection, and a remote-only control that shows which sources honor it. Add employment type and radius with source-specific validation. The existing Ohio eligibility, compensation, and fit policies would still govern recommendations, even when a search uses a different location or work arrangement.

Salary range, seniority, and separate hybrid/onsite choices are not dedicated filters in the inspected `scrape_jobs` signature. Those would require query text or filtering returned listings, with missing values handled explicitly. `enforce_annual_salary` converts returned pay figures; it does not filter by pay. Description fetching and formatting, proxies, certificates, user-agent settings, and the MCP timeout are retrieval or operational settings. Google is excluded from the local MCP site list and is documented upstream as unavailable.

## Checkpoints and cancellation

`hunts` stores the request and stage. `hunt_leads` stores each discovery, evidence, child job, outcome, and reason. A coordinator tick performs a bounded step and releases the worker so search, evaluation, and rendering jobs can run. Child jobs take priority over coordinator polling. Search request keys prevent a restart from queuing the same MCP search twice.

Pause stops scheduling the next task and holds queued children. A current child may finish. Resume continues from saved state. Cancel requests cancellation of active children and retains their completed evidence. A cancelled run is not restarted automatically. A failed coordinator can retry from its checkpoint.

Codex evaluations have schema-constrained evidence IDs and rubric categories. Validation rejects unknown citations, missing categories, weakened central qualifications, and stale evidence. One repair attempt is permitted for a rejected evaluation. A second failure remains visible in the run; it is never counted as a recommendation. Language-model confidence does not establish facts or waive a gate.

## Data destinations

Live queries, raw JobSpy results, errors, and manifests are saved under the connected workspace's `_Research/Agent-<search-id>` directory. Official responses and normalized captures stay under private state in `official-raw` and `official-captures`. New application proposals live in PostgreSQL, separate from imported tracker rows.

Prepared real packages are previews under `JSA_STATE/prepared-workspace`. They do not overwrite an existing application folder. The corpus exposes these files through the `_AgentGenerated/` namespace, with `generated_draft` authority. They remain excluded from factual retrieval by default. Contact information is added locally by the renderer; Codex receives selected career evidence and official posting text through the enabled account. It does not receive the entire corpus.

The intake adapter imports the explicitly confirmed technical-experience section of `job_search_workflow.md`, with exact quotes and document versions. It excludes the BAM-specific commitment paragraph. Repeated confirmation records are deduplicated in model evidence packets. Existing contradictory or historical records do not gain authority from repetition.

## Official-opening support

The verifier supports current Greenhouse, Lever, and Ashby public feeds. It also follows public ATS links from discovery pages and handles employer-owned JSON-LD, Breezy, and iSolved postings when employer identity, requisition, current description, and an application route can be established. Greenhouse board names must match the discovered employer. Expired structured postings are rejected. Unsupported portals, inaccessible pages, redirects without usable evidence, and identity mismatches remain unverified.

This is bounded verification, not a claim of universal portal support. The agent does not solve CAPTCHA, bypass a rejected browser, or infer Ohio eligibility from a city label. A U.S.-remote posting can remain a qualified match with Ohio eligibility unspecified; the review displays that distinction.

References checked during implementation:

- [MCP Python SDK client transports](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/transports.md)
- [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html)
- [Lever Postings API](https://github.com/lever/postings-api)
- [Ashby public job postings](https://developers.ashbyhq.com/docs/public-job-posting-api)

## Approval boundary

The live path can proceed from researched, scored packages to reviewed form answers and approval-driven submission through the supported Lever and Greenhouse adapters. The visible browser, missing-answer flow, payload-bound approval, submission checkpoint, receipt verification, and tracker synchronization are exercised against local employer fixtures. A ready package is not a submitted application or an authorization.

The sending adapters have fixture coverage; production acceptance has not yet been exercised with a real submission. Unsupported portals and form controls still require intervention. Imported ODS statuses and existing workspace documents remain unchanged.
