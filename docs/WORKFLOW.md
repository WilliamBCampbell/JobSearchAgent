# Application steps and recovery

The application page shows one current step and one main action. Completed work stays available in collapsed sections. Sections needed for the current step open automatically. Moving to another step folds the previous sections; their summaries remain clickable so the user can revisit them. Manual opening and closing persist through background refreshes until the workflow changes step or the user opens another application. If a gate needs attention again, later completed work is labeled **Saved**. A fit update does not regenerate documents or erase answers.

| Step | What completes it | Next action |
| --- | --- | --- |
| Check the role | Current official opening, fit assessment, and role selection | Select the role, refresh the opening, or resolve the listed fit concerns |
| Prepare and review documents | A generated package and the user's review of that exact version | Request changes or choose **Documents look good** |
| Prepare answers | Required answers confirmed, supported form checked, and current answers and files filled | Answer missing questions or choose **Continue to application form** |
| Review and approve | Explicit approval of the matching answers, attachments, salary, and consents | **Approve & send** |
| Send application | The approved worker run completes or pauses for employer verification | Follow the verification instructions if needed |
| Confirm receipt | Captured employer confirmation or explicit user confirmation | Review the receipt and tracker preview |

Conditional selection permits document preparation. It does not waive the sending checks. The fit panel explains remaining requirements and permits corrections before submission. Confirming a candidate qualification cannot establish an employer's Ohio eligibility policy. Scores remain judgments, not hiring probabilities.

## Implementation

`progress.py` projects the preparation checkpoints into the next step, action, and status. The API includes this projection in each application response; the page and intervention inbox use it. Reading progress does not change workflow state. Application responses include the computed review, so the UI does not fetch it again. Polling waits for each refresh to finish; older responses cannot replace newer state. Submission, verification, and receipt recovery retain their separate attempt-bound controls.

`Workflow.continue_prepared` checks the current opening, fit, document review, pause state, and absence of a submission attempt. It can restore a selected application to its existing prepared checkpoint. It checks selected files before restoring the checkpoint. The form planner refreshes answer evidence before browser filling. Continuing never starts document generation. Repeated package jobs reuse the published version; deliberate revisions use the version-bound revision endpoint.

Scoring and requirement decisions preserve selected, prepared, answer-waiting, and review checkpoints. They invalidate submission approval. An authorized application returns to review when a requirement decision changes. New qualifications discovered while filling still require fit reevaluation. Contact fields use the same exact-label mapping as the profile-answer service. Required contact, compensation-answer, and personal-narrative controls remain form answers rather than career qualifications. They still require valid answers before approval. Genuine career requirements remain in the assessment.

The opening-refresh job uses the established public employer adapter. An unchanged capture allows deterministic recalculation of the existing assessment with current verification. Changed content requires rescoring. Failed verification retains the URL for another check without retaining a claim that the opening is open. Verification and scoring cannot rewrite an attempted application's outcome.

Fit evaluation cache identity includes the actual facts, source versions, requirements, policy, and a hash of material consents. Unrelated state revisions do not invalidate it. Private consent values are not added to model context. A successful job recovery clears its current error; prior inference failures remain in their audit records.

An authorized run that stopped before recording any attempt can resume with the same matching approval. Once an attempt exists, reconciliation controls apply and answer edits remain blocked.

Paused applications retain queued jobs. The worker skips them until the user resumes. Document review stops automatic search preparation before browser filling, and the approval endpoint independently checks the reviewed package version.

## Validation boundary

Use the synthetic browser workflow in `tests/test_sequential_journey.py` and checkpoint cases in `tests/test_progress.py`. Existing browser suites cover employer-form changes, verification challenges, immutable approval payloads, missing receipts, and interrupted attempts. These tests do not submit real applications or establish live employer acceptance.

## Remote roles, compensation, and application questions

Under scoring policy `2026-10-20.remote-pay.1`, a U.S.-remote role does not need to name Ohio. Missing state-specific wording remains unconfirmed evidence but causes no deduction or qualification gate. Explicit location exclusions and travel, relocation, onsite, or schedule requirements still apply. Unlisted compensation also causes no deduction or gate. A required salary-expectations answer uses the candidate's application-specific answer; it does not become an employer salary offer. Disclosed low compensation retains the existing fit rules.

A confirmed application-scoped `employer_driving_distance_miles` fact between 0 and 100 gives two ranking points. The bonus is separate from the 100-point qualification rubric and cannot make an ineligible role primary. Missing, conflicting, global, or unconfirmed distances earn no bonus. Search ranking uses this preference; a city label alone cannot establish driving distance.

Open-ended requests for project examples, GitHub contributions, or descriptions of agentic-AI work are application questions. They remain required answers but do not create qualification gaps on their own. The assessment still evaluates separate posting qualifications and explicit tenure or credential thresholds. Answer preparation uses confirmed evidence, asks for missing details, and cannot promise a demonstration of private work without evidence that it can be shown.

## Narrative prompts with choice-only controls

Question intent is identified separately from the employer's control type. A request for project examples can remain a narrative request even when the employer configured a Yes/No dropdown. The app preserves that dropdown's exact options and never infers a choice from a proposed narrative.

The answer editor provides application-scoped notes and a separate employer choice. **Draft examples from my records** queues the existing evidence-backed answer writer and reviewer against a text version of the question. A draft remains unconfirmed until the user saves it. Stale evidence hides the draft. Notes are stored separately from employer answers and are excluded from the submission payload; they can inform subsequent answer preparation for this application through the configured provider. Selecting Global applies to the employer choice, not to the private context notes or interview-demo permissions.

Ordinary narrative questions with a text control receive a multiline editor. Explicit tenure and credential thresholds remain qualification checks. Interview demonstrations still require evidence that the candidate can show the work; prior employment alone does not establish permission to share private code.
