# Requirement decisions and rescoring

Open an application's **Fit assessment** to review its central required qualifications. Each unresolved item has **Confirm**, **Deny**, and an optional quick note. The same controls appear below an applied fit assessment in Evaluations. The original model result remains a saved snapshot; the controls update the current application assessment.

Confirming or denying an item removes it from the unresolved list. Resolved requirements stay hidden until you choose **Show resolved requirements**, where you can review notes or change a decision. Each requirement appears once in the active review; separate eligibility and score warnings remain visible.

In Find roles, expand **All discoveries and decisions** and choose **Review fit** to open an evaluated job. The table retains the search's original decision and shows the current fit score separately.

Confirm means the candidate meets the exact requirement. It records an application-scoped confirmation, marks the requirement supported, and removes that item's deduction. Deny means the candidate does not meet it. The question is resolved, but the qualification gap remains and blocks a primary recommendation. Confirm does not mean the employer requirement is optional, and Deny does not remove it from the posting.

Both actions save the note, source, requirement identity, prior score hash, and a confirmed decision fact. Changing a decision supersedes its earlier fact and invalidates dependent answer caches. Other applications and global career facts are unchanged. Requirement identity includes the wording, source, category, required/preferred classification, and central/material flags. Changed employer requirements need a new decision.

The score is recalculated immediately after a decision. Official-opening verification, Ohio eligibility, compensation rules, remaining qualification gaps, and score minima still apply. Any previous submission approval is invalidated, and the application returns to fit review. Decisions never select a role, prepare documents, or authorize sending.

## Rescore job

**Rescore job** queues a fresh fit evaluation through the configured provider and model route. When Codex is enabled, this uses the existing monitored `codex exec` integration. The input includes current career evidence, employer requirements, and the saved scoped decisions and notes. The offline provider supports fixture checks without a hosted call.

The durable worker applies a valid result to the application without advancing it beyond scoring. Code reapplies exact matching user decisions after the model assessment, so a model cannot erase a denial or treat one confirmation as evidence for an unrelated requirement. Results based on older evidence or decisions are rejected. Concurrent clicks return the same active job; retries are bounded, and cancellation before application prevents the score update. Progress and failures appear in the application's work list.

Rescoring is available during fit review before selection or submission. Imported tracker records and attempted applications cannot be changed through these controls. A stale browser view must refresh before submitting a decision. Resolving all central questions does not guarantee a primary recommendation.

Migration 009 stores decision history in PostgreSQL. Existing backup and restore commands include this table and its linked facts. Unresolved central requirements appear in **Needs attention**; an explicit denial clears the question notice while preserving the fit gap.

## Validation

`tests/test_requirement_decisions.py` covers confirmation, denial, optional notes, reversals, cache invalidation, scope isolation, changed requirements, stale views, approval invalidation, independent eligibility gates, imported-record protection, rescoring with saved notes, and stale model results. Its browser test checks that either decision hides the requirement, resolved items remain hidden after reload, the toggle reveals saved notes, and duplicate validation messages are absent. It also runs an offline rescore without creating a submission attempt.

## Selecting a role after assessment

The application now shows a **Role selection decision** panel before the detailed fit evidence. Primary roles have a direct selection action. Conditional roles show the saved reasons and non-central uncertainties, then require a short reason and an explicit acknowledgment before **Select conditional role** becomes available. Resolved central requirements remain in their existing review controls rather than being repeated in this panel.

A high arithmetic score does not guarantee primary status. Two material unconfirmed requirements can keep a role conditional even when all central requirements are resolved. Findings about application answers describe the saved assessment; form preparation checks current scoped memory before asking the user again.

Selection moves the workflow to document preparation. It does not resolve gaps, change the score, confirm salary or other answers, or authorize submission. The existing automated sending gate still requires a primary assessment. Use evidence correction and rescoring before selection to resolve that gate, or use a reviewed manual handoff for a role that remains conditional.

The API returns `selection_review` with the available path, blockers, findings, and a review hash. The UI sends that hash when selecting. The server rechecks it under the application lock and records the conditional reason in the selection audit event. Empty reasons, skipped assessments, missing assessments, paused workflows, imported records, and stale reviews cannot bypass the selection controls.

The journey labels each step as current, complete, or not completed. Scoring alone does not complete role selection, and an attempted send without receipt does not display as a completed sending step.
