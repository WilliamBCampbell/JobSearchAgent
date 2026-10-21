# Autonomous search and preparation

Open **Find roles** and choose **Find my best matches**. The app searches real Indeed and LinkedIn listings through the local JobSpy MCP server. The worker then checks official openings, compares prior applications, extracts requirements, evaluates fit through Codex, ranks qualifying roles, and prepares up to five packages. The screen survives reloads and shows saved progress, source warnings, exclusions, and package previews.

The default run examines up to 60 discovery groups and evaluates up to 12 verified openings. These bounds and the primary limit are typed fields in `HuntRequest`. Software engineering, applied AI, and solutions engineering use separate queries; solutions engineering retains its separate rubric. Results beyond the configured bounds remain in the saved search results. No batch is padded to reach five.

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
