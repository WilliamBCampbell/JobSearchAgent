---
name: career-memory
description: Look up, correct, and scope confirmed career facts before retrieving documents.
---

# Career memory

Inputs: candidate, question key, and application ID when applicable.

Use `POST /api/answers/resolve`. Its order is current context, confirmed structured facts, valid cache, retrieval, then a user question. Inspect fact IDs and source references before using an answer. Use `POST /api/facts` for a user correction with `supersedes` pointing to the original fact.

Salary, travel, relocation, schedules, references, consent, and authorization belong to one application. General preferences do not answer commitment fields. Record skill durations separately from the presence of a skill. Do not turn model confidence or repeated drafts into confirmation.

Output: an answer with provenance or a concise unresolved question. Stop for conflicting evidence or a missing material commitment. Verification codes, passwords, and tokens belong in the browser, never memory.
