# External submission migration

The preparation UI remains the main application. It inspects employer forms, extracts fields, and prepares answers. Form entry, uploads, submission, and browser verification continuation move to a separate Codex CLI conversation using a generated prompt and a private application packet.

## Repository split

The prior implementation is preserved in [JobSearchAgentSubmission](https://github.com/WilliamBCampbell/JobSearchAgentSubmission). That repository is archived. [JobSearchAgent](https://github.com/WilliamBCampbell/JobSearchAgent) continues from the full submission-era source checkpoint, including the previously uncommitted preparation and profile work. The repositories share their earlier Git history.

The active local checkout stays in place so existing Python environments and service paths continue to work. A separate legacy checkout preserves the retired source. Database records, profile folders, generated documents, and source workspaces are not copied into either GitHub repository or reset during the split.

## Responsibilities

| Stage | Owner |
| --- | --- |
| User profiles, document ingestion, job discovery, fit evaluation | App |
| Resumes, cover letters, research, suggested answers, document review | App |
| Open and inspect employer applications, extract fields, generate profile-based answers | App |
| Verify the current form, enter answers, upload files, submit | Codex CLI and its browser tools |
| Human verification and missing personal information | User in the Codex conversation or employer browser |
| Receipt review and reviewed tracker updates | App |

The app must never submit, even if an old deployment file or environment variable enables sending. Old API actions and queued submission, reconciliation, and verification jobs must fail without browser execution. The worker still opens the official application for read-only inspection, extracts fields, and generates answers from the selected profile. Existing inspection supports Lever, Greenhouse, and Breezy through employer HTML or public form definitions; unsupported portals remain explicit handoffs.

## First delivery

1. Add a private, versioned packet exporter shared by the UI, CLI, and MCP.
2. Copy the exact selected documents and record their hashes and absolute paths. Later document edits must not change an exported copy.
3. Include the selected profile, current confirmed facts, source context, official posting, prepared answers, questions needing review, consents, and previous attempts and receipts. Keep drafts and source claims distinct from confirmed facts. Exclude other profiles and unrelated sensitive facts.
4. Generate a prompt for the user's existing browser tools. No per-employer package installation or application code repair belongs in that prompt.
5. Make submission intent explicit. New applications can be completed when the user asks Codex to execute the prompt. Unknown previous outcomes require reconciliation. Already recorded or closed applications are reference-only.
6. Replace the final send button with prompt generation and copying. Include each extracted question and its prepared answer directly in the copied prompt. Provide separate copy controls for each question, answer, pair, and the complete answer set for manual entry. Preserve preparation, review, and receipt entry.
7. Disable the retired execution routes at queue admission and worker dispatch. Preserve prior attempt and approval history.
8. Verify using synthetic applications, document changes, prior attempts, profile isolation, and a browser UI test. Do not submit a real application during development.

The packet lives under the selected profile's private state in `external-handoffs/<application-id>/<packet-id>`. It contains `application.json`, `prompt.md`, `manifest.json`, document copies, a result template, and a writable `results` directory. The prompt names absolute local paths. Execution does not require the API or worker to stay running.

Proposed user workflow:

1. Prepare and review the application in the UI.
2. Select **Create submission prompt**, then **Copy submission prompt**.
3. Paste it into Codex CLI and ask Codex to complete the application.
4. Answer new questions or complete employer verification in that conversation.
5. Return to the app and record actual employer confirmation before reviewing a tracker update.

CLI export: `jobsearch handoff --profile PROFILE_ID --application APPLICATION_ID`.

MCP export: `export_submission_handoff(application_id, reviewed_hash)`, bound to the MCP process's selected user profile. Exporting creates no submission attempt and grants no ability to the app to send.

## Delivery status

The first delivery implements packet export through the API, UI, CLI, and MCP. It retains employer field inspection and answer generation, adds individual and bulk clipboard controls, and disables the retired send routes. Synthetic tests cover snapshots, evidence changes, AI draft validation, prior attempts, profile isolation, queue rejection, MCP export, and browser clipboard behavior. The UI production build is checked separately.

The predecessor's native execution tests still describe its sending contract. They are not evidence for the external submission workflow; their retirement is part of the follow-up cleanup.

## Follow-up deliveries

- Add a reviewed result importer that validates application ID, profile ID, packet ID, document hashes, and evidence paths. An AI status string alone must not mark an application received.
- Add preparation and application-context MCP tools so a CLI conversation can run more of the workflow without replacing the UI.
- Select and pin a browser-tool version, document setup once, and test a persistent browser session per user profile. CAPTCHA and employer-site failures still require the normal employer workflow or human intervention.
- Remove obsolete native sender modules, deployment toggles, UI copy, and tests that describe the archived execution contract. Keep useful preparation and history readers.
- Exercise recovery after a machine restart using the exported packet and saved result, without requiring an application-worker browser session.

## Validation boundary

A generated packet is preparation evidence. A successful file export, browser click, HTTP response, or public thank-you page is not employer receipt. Unknown outcomes prohibit another send until reconciled. No step in this migration deletes or invents historical receipt evidence.

The first delivery retains manual receipt entry. Automatic result import and a pinned browser installation are follow-up work, not implied capabilities of the initial exporter.
