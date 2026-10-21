# Workspace integration contracts

Personal workspace contents are not part of this repository. The intake service writes a private inventory with paths, hashes, extraction results, and source authority. Import original files without rewriting them.

| Component | Contract |
| --- | --- |
| JobSpy adapter | A separate checkout with `search_jobs.py`, `search_profiles.json`, `build_plan`, `deduplicate`, a JSON worker mode, and its own Python environment |
| Career documents | Supported files under `JSA_WORKSPACE`; confirmed facts must carry explicit confirmation provenance |
| Application tracker | The supported eight-column ODS Applications layout; imported statuses remain authoritative |
| Document rendering | LibreOffice, Poppler tools, and fonts; outputs go to private version directories |
| Employer automation | Playwright adapters with exact field contracts, per-application approval, attempt checkpoints, and receipt reconciliation |
| Writing instructions | The complete no-ai-slop skill and reference guide, installed separately or selected with `JSA_NO_AI_SLOP_SKILL` |

The importer recognizes workspace handoffs, workflow records, original resume sources, confirmed answer records, and submission receipts. Generated prose cannot confirm career facts. Explicit corrections supersede earlier claims; filenames and modification times do not determine truth.

Separate requisitions, deferrals, prepared packages, and unresolved submissions remain distinct. Salary, relocation, travel, schedule, reference permission, and submission authorization retain their application scope. The tracker adapter does not claim to support arbitrary spreadsheet layouts without a mapping.
