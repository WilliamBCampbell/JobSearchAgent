---
name: tracker-handoff
description: Reconcile an application outcome with the tracker and prepare a manual handoff or follow-up report.
---

# Tracker handoff

Inputs: current ODS hash, application identity, state, actual attached documents, and receipt evidence.

Read the current spreadsheet before planning updates. Real writes require deployment enablement and a reviewed preview; development tests use isolated workspaces. Use the tracker synchronization API, which locks, checks the expected hash, backs up the archive, preserves unrelated rows and members, writes a recovery journal, and atomically replaces the file. Inspect conflicts instead of overwriting external edits.

Use `jobsearch recover --candidate real` or `--candidate fixture` for incomplete journals. Keep unresolved submissions separate from ordinary unanswered questions and deferred applications. Preserve distinct requisitions at one employer. A manual-application queue reaching ten triggers one report until it drops below ten; an on-demand report is available at any count.

Output: consistent tracker totals, notes, checklist, handoff, and an audit entry. Stop on an ambiguous row, changed file hash, conflicting status, or absent receipt for submitted state. Do not reuse old authorization to retry an unresolved submission.
