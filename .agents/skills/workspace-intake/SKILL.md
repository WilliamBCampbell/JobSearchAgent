---
name: workspace-intake
description: Import an existing job-search workspace and reconcile the provenance of career records.
---

# Workspace intake

Inputs: the configured workspace, its workflow and handoff, the ODS tracker, and confirmed answer records.

Use `jobsearch inventory`, then `jobsearch import --candidate real`. Read the private intake report and unresolved conflicts. The import reads originals and writes only private application state. Full corpus extraction is a separate `jobsearch ingest` operation.

The ODS controls imported application status. A prepared row can also have a deferred decision. Preserve both. Resolve title, employment-date, and skill-duration disagreements using an explicit correction and its source section. A newer filename is not evidence of a correction. Original resumes remain historical where corrected.

Output: an inventory, source references, imported statuses, and unresolved conflicts. Stop on an ambiguous tracker identity or unsupported factual correction. Never replace the real tracker during development.
