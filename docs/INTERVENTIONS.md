# Manual answers and application notifications

The **Needs attention** page lists applications waiting for user input. The sidebar shows the number of affected applications, and a banner shows unread notices throughout the app. The inbox follows the selected workspace, so fixtures and real records remain separate.

Required answers enter a manual handoff when current confirmed evidence is absent, conflicting, expired, or incompatible with the employer's answer choices. The agent saves the outstanding questions and available answers in a private HTML/JSON handoff. Each question explains why confirmation is needed. Generated drafts and retrieved text do not establish confirmation. Optional unanswered fields stay blank and remain visible in the final review.

Questions about personal interests, motivation, values, or aspirations receive a distinct **Your personal perspective is needed** label when confirmed personal context is absent. Relevant career evidence remains visible, but it cannot fill in a personal motive. A mixed question stays unresolved until the candidate supplies that part. If personal context is already confirmed, the app may offer a reviewed draft for editing and confirmation. Factual questions first use the same current career records as the resume writer, with retrieval and a separate evidence review when needed.

The application retains its `missing_answers` state so answering the last question can queue preparation again. This is a manual answer handoff inside the app. The separate `manual_handoff` state still covers applications that need completion in a normal browser. Neither state permits submission. After answers are confirmed, form inspection and any qualification checks run again before review and per-application approval.

For reusable factual questions, the **Use this answer for** radio buttons offer **This application only** and **Global**. New answers default to this application. Explicitly confirmed global answers can be reused when the employer question's wording, field name, and options match. Salary, travel, relocation, references, consents, and other role-specific commitments show a **This application only** notice with an explanation instead of disabled radio buttons. The answer can still be entered and confirmed. The API enforces the same scope restriction.

Confirming an answer replaces conflicting facts within the chosen scope. Choosing Global also replaces prior answers to that question in the current application; overrides saved for other applications remain intact. Choosing This application only leaves the global answer intact. Superseded facts retain their history; their caches and dependent approvals are invalidated. Confirmation cannot authorize sending.

An explicit answer to a recognized work-authorization or visa-sponsorship status question can be saved globally. These questions describe the candidate's status; they do not grant submission authority. Reuse requires matching field identity, wording, and options. Added consents and different country-specific questions do not inherit the exception. The agent still needs an explicit confirmed answer and cannot infer it from career experience.

## Inbox behavior

Each notice shows the initial score, its date and source, and the current score. New assessments preserve their first applied score in the audit log. Older records use the earliest saved completed evaluation when available, labeled **First saved evaluation** because it may precede the applied assessment. Missing history displays **Not recorded**. Rescoring cannot replace this history or make an unchanged notice unread.

The card shows up to three current requirement concerns. Mandatory disqualifying gaps come first, followed by central required qualifications, other requirements, and preferred qualifications. Each concern includes its assessment, deduction, and expandable explanation. Confirmed requirements leave this list; denied qualifications remain visible as gaps. The full assessment retains the remaining concerns.

**Discard** replaces the Defer button on the application page and is also available in the inbox. It records the role as discarded, cancels queued application work, invalidates approvals, resolves outstanding notices and follow-ups, and removes the role from search coordination. Assessments, answers, documents, and decision history remain available. This action does not write the ODS tracker. Existing deferred records retain their status. Imported records, active tasks, and applications with submission attempts or receipts cannot be discarded here; the page explains the blocker.

Notifications cover missing answers, browser verification, unsupported portal handoffs, unresolved receipts, exhausted task retries, paused applications, and applications ready for review. Existing manual application follow-ups also appear, including prepared applications whose tracker status does not describe their blocker. Imported records are visible without changing their authoritative tracker status. Separate requisitions remain separate inbox entries.

**Mark as read** records acknowledgment only. The notice remains open until its underlying condition changes. Repeated polling does not duplicate notices or make an unchanged notice unread again. A changed question set or a reopened blocker becomes unread. Finalized and deferred applications leave the active inbox. A new failed job of the same kind replaces the prior job notification; a queued retry is visible in the application's task list.

Migration 008 stores notification read/resolution timestamps and question reasons in PostgreSQL. The API projects current application, question, and latest task states into the inbox on each refresh. The UI polls every two seconds; opening it after a process restart rebuilds any missing notices while preserving acknowledgments. This projection cannot change application status or approvals. Resolved notices remain available through `GET /api/notifications?include_resolved=true`.

The inbox and its acknowledgment endpoint use the existing local API authentication. Notice content contains question prompts and evidence identifiers, not verification codes or model reasoning. Notification delivery is in-app only; email, operating-system push, and mobile delivery are not implemented.

Answer handoffs also enter the existing manual application follow-up list. Confirming the outstanding answers or deferring the application clears that handoff entry. The ten-item report threshold and on-demand report remain in place.

## Validation

`tests/test_interventions.py` checks uncertain and conflicting answers, expired facts, incompatible choices, saved handoffs, scoped corrections, persistence across service instances, duplicate suppression, acknowledgment without authorization, challenge and receipt notices, task retries, authentication, and imported-record preservation. Its browser test opens the inbox, reloads after acknowledgment, opens a question, and confirms it without creating approval or submission records.

With the local services running, reproduce the fixture demonstration with:

```bash
.venv/bin/python scripts/demo_interventions.py
```

It creates a new synthetic application, waits for its missing-answer notice, marks that notice read, confirms the fixture salary answer through the UI, and stops at review. Screenshots and `intervention-demo.json` are saved under `JSA_STATE/demo-evidence`. The script checks that the real tracker hash, imported real records, and real attempt count are unchanged.
