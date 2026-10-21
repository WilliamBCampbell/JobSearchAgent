# Codex inference jobs

The application sends language-model work through `InferenceService`. Embedding providers are independent. Codex does not generate embedding vectors. The configured deployment uses local lexical retrieval; Codex can propose alternate search terms and evaluate retrieved passages. Those search terms are available as search buttons in Evaluations. Query expansion is not learned vector retrieval.

## Task routing

| Task | Default model | Effort | Reason for starting here |
| --- | --- | --- | --- |
| Query expansion | `gpt-6-luna` | low | Short searches with a narrow output contract |
| Requirement extraction | `gpt-6-luna` | medium | Preserve explicit posting language and classifications |
| Evidence selection | `gpt-6-luna` | medium | Select from a bounded list of confirmed facts |
| Fit assessment | `gpt-6.1-sol` | medium | Compare requirements, evidence, gaps, and rubric deductions |
| Drafting | `gpt-6.1-sol` | medium | Prepare text with references for human review |
| Conflict review | `gpt-6-astra` | high | Examine contradictory statements and correction provenance |

These are starting choices, not measured winners on this corpus. The UI allows a model and effort override for each task. A provider failure never silently changes the selected model, spends on a stronger fallback, or turns an inference into a confirmed fact. Catalog listings are advisory; the account can still reject a model.

The choices follow [OpenAI model-selection guidance](https://developers.openai.com/api/docs/guides/model-selection), checked October 19, 2026. Luna is intended for scoped tasks, Sol for work requiring judgment with attention to cost and time, and Astra for demanding analysis. The installed CLI catalog listed the configured models. Compare the same evidence packets across routes before calling a choice optimal.

## Run locally

Install and sign into Codex using its supported CLI setup. This implementation was exercised with `codex-cli 0.163.0`; it checks no credentials into Git and does not read or copy the authentication store. It invokes the installed `codex` executable. The transport depends on Linux parent-death signaling and the tested CLI flags; rerun the transport tests when upgrading Codex.

1. In **Privacy & settings**, choose **Codex** under **Codex evaluations**.
2. Enable sending selected evidence to OpenAI and save. The current deployment was enabled at the user's request; a fresh installation defaults to offline fixtures.
3. In **Evaluations**, choose a task and, where needed, an application. Fit assessment needs a captured posting. Source version IDs can select additional documents.
4. Open the evaluation history entry to watch events and inspect the input packet, result, usage, and errors. Cancel or retry through the displayed controls.
5. Review a fit result before choosing **Use reviewed assessment**. The scorer recomputes eligibility. Imported application statuses remain read-only.

`GET /api/llm/catalog` reports CLI version and model names without returning the raw catalog. `PUT /api/settings/llm` configures the backend and routes. `POST /api/llm/jobs` accepts this contract:

```json
{
  "task": "fit_assessment",
  "candidate": "fixture",
  "application_id": "APPLICATION_ID",
  "question": "Evaluate the required qualifications and identify gaps.",
  "source_versions": []
}
```

The response contains a durable `job_id`. Read `/api/jobs/JOB_ID` and `/api/llm/runs/RUN_ID` for progress and output. All endpoints use the application's existing local authentication and origin protection. `/api/jobs/JOB_ID/cancel` stops a queued or running task; `/resume` retries a failed or cancelled task with current evidence. No MCP server is needed to invoke Codex.

Run the synthetic demonstration with:

```bash
.venv/bin/python scripts/demo_inference.py
```

It uses the configured provider and therefore consumes Codex account usage when Codex is enabled. It evaluates synthetic material, saves a private report, and performs no application submissions.

## Modules and contracts

- `llm/contracts.py`: typed requests and task-specific results. Adding a task requires an explicit contract, route, instructions, and validation.
- `llm/routing.py`: backend settings, model choices, and a reduced CLI catalog.
- `llm/runner.py`: subprocess execution, JSON event monitoring, timeout, output limits, and cancellation.
- `llm/supervisor.py`: kills the Codex process if its worker dies. The CLI runs without the shared daemon.
- `llm/service.py`: scope checks, evidence packets, result validation, version-aware caching, and reviewed score application.
- `.agents/skills/llm-evaluation/SKILL.md`: task instructions embedded in each prompt and included in the cache identity.

The existing package workflow uses this service for evidence selection. Rendering remains extractive. Freeform drafts are review proposals; they are not automatically attached or used as confirmed answers. Embedding changes do not change the inference backend, and model changes do not require re-ingesting documents.

## Execution and privacy

The runner uses `codex exec --json --output-schema`, with a read-only sandbox and shell, web search, plugins, and delegation disabled. It ignores user configuration, rules, and host skill discovery for the evaluation process, supplies the repository's bounded skill directly, and fixes the model provider to OpenAI. This does not modify the user's Codex configuration. See [non-interactive Codex documentation](https://learn.chatgpt.com/docs/non-interactive-mode).

The packet contains applicable confirmed career facts, selected document passages, and application context. Query expansion receives only its question and task metadata. Structured contact fields and role commitments are excluded from the career-fact packet. Package identity fields are added locally after selection. Source passages can still contain personal information; inspect the packet when that matters. Passwords and session tokens are never requested as career evidence. Employer text cannot authorize a tool or state change. Reported tool activity causes rejection; the subprocess receives no database URL or local API token.

Selected evidence is sent to OpenAI through the saved Codex login. Account limits and the account's data policies apply. The whole corpus is not uploaded. The private database stores the submitted evidence packet and validated result for inspection. Ephemeral CLI runs do not retain conversation rollouts; temporary raw result files are removed. Raw stderr and reasoning events are not retained. Operational events record lifecycle, selected model, elapsed time, and numeric usage fields. No dollar estimate is fabricated from token counts.

Only one Codex process can run at a time across workers, enforced by a PostgreSQL lock. Execution defaults to a 240-second timeout, a 65,000-character evidence-packet budget, a 2 MB event-stream limit, and a 150 KB result limit. Oversized packets fail visibly instead of silently dropping evidence. Retries are bounded by the durable queue. Provider, schema, citation, or stale-evidence errors stop for review; there is no automatic model escalation.

Cancellation terminates the process group. Worker death kills the Codex process, and lease recovery marks the old run interrupted. Lease recovery requeues an interrupted job within its attempt budget unless it was cancelled. A resumed job starts a fresh read-only evaluation; completed matching results can be reused. This can consume usage again after an interruption. It never repeats an application submission. Cancellation cannot undo provider usage already incurred.

## Evidence and cache validity

A cache identity includes task input, application context, confirmed facts, source versions, policy, selected model and effort, output schema, and skill content. Corrections or changed indexed evidence create a different identity. Results are rejected if evidence or configuration changes during execution. Sources must belong to the selected candidate; facts must be current and global or scoped to that application. Existing central required qualifications, including captured form requirements, cannot be omitted or weakened by a new assessment.

The validator checks structure and evidence IDs, not the truth of every model interpretation. A cited fact can still be misinterpreted. Draft prose and fit judgments need review, and model responses cannot promote facts into confirmed memory. The code recomputes scores, enforces mandatory gaps, and requires separate per-application authorization and receipt evidence.

The automated tests use synthetic inputs and a fake executable through the real subprocess transport. They do not require model calls. Live demonstration results are reported separately in `EVALUATION.md`.
