---
name: jobspy-search
description: Search with the established JobSpy adapter and inspect source health, progress, and result provenance.
---

# Jobspy search

Inputs: search track, saved profile, filters, and fixture or explicitly enabled live mode.

Use the JobSpy MCP tools `health`, `list_profiles`, `start_search`, `search_progress`, `search_results`, `cancel_search`, and `normalize_results`. The same contracts serve the guided UI. Retain the durable job ID and inspect every query's manifest.

An empty successful query differs from failure. Keep partial results, rate limiting, missing descriptions, parser errors, and ignored filters visible. This checkout ignores LinkedIn's remote filter. A city label does not establish a remote restriction. Never bypass CAPTCHA or source protections.

Output: source-attributed discoveries, raw artifact paths, warnings, and duplicate candidates. Stop or cancel bounded searches when sources block access. Saved listings do not establish current availability.
