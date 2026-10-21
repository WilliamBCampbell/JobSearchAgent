---
name: approve-and-receipt
description: Review an application-specific payload, obtain explicit approval, and verify receipt without duplicate attempts.
---

# Approve and receipt

Inputs: employer, role, requisition, final answers, material consents, and actual attachment hashes.

Present the final guided review. The user's application-specific approval calls `POST /api/applications/{id}/approve` with the reviewed hash. Code checks matching answers, form schema, attachment contents, and current evidence. A material change invalidates approval. MCP exposes no submission tool. Real sending stays disabled during development. Inspect the employer notices and selected documents in the local plan before approval.

After approval, complete the approved application on a supported, enabled adapter without asking again unless its scope changes. The attempt is committed before typing or attaching to a real employer page, or before the fixture final action. If interrupted afterward, use reconciliation; do not enqueue another submission. An employer receipt or explicit user receipt confirmation is required for submitted state.

Output: immutable submitted payload and attached versions, receipt evidence, or an unresolved outcome. Stop for a missing receipt, mismatch, challenge, or uncertain prior attempt. Outreach needs separate explicit authorization and is not implemented by this skill.
