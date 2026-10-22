# Workspace integration contracts

Personal workspace contents are not part of this repository. The intake service writes a private inventory with paths, hashes, extraction results, and source authority. Import original files without rewriting them.

| Component | Contract |
| --- | --- |
| JobSpy discovery | Official `mcp-jobspy` 1.4.0 in an isolated pinned environment, or the retained legacy JSON worker; app-owned presets and deduplication; see [comparison and setup](JOBSPY_MCP_COMPARISON.md) |
| Career documents | Supported files under `JSA_WORKSPACE`; confirmed facts must carry explicit confirmation provenance |
| Application tracker | The supported eight-column ODS Applications layout; imported statuses remain authoritative |
| Document rendering | LibreOffice, Poppler tools, and fonts; outputs go to private version directories |
| Employer applications | Read-only form inspection and answer preparation in the app; external Codex handoff for form entry, uploads, and submission; reviewed receipt entry |
| Writing instructions | The complete no-ai-slop skill and reference guide, installed separately or selected with `JSA_NO_AI_SLOP_SKILL` |

The importer recognizes workspace handoffs, workflow records, original resume sources, confirmed answer records, and submission receipts. Generated prose cannot confirm career facts. Explicit corrections supersede earlier claims; filenames and modification times do not determine truth.

Separate requisitions, deferrals, prepared packages, and unresolved submissions remain distinct. Salary, relocation, travel, schedule, reference permission, and submission authorization retain their application scope. The tracker adapter does not claim to support arbitrary spreadsheet layouts without a mapping.
