---
name: browser-application
description: Fill a selected application in the visible worker and handle user takeover or missing answers.
---

# Browser application

Inputs: prepared package, confirmed scoped answers, and verified route.

Resolve recognized profile fields from the same confirmed contact records used by resume rendering, including optional fields and custom employer field names. Use a confirmed no-employee-referral rule only for applications prepared through this app. Do not infer a discovery source from that rule. Preserve local overrides, exact question identity, and evidence checks before approval.

Use `POST /api/applications/{id}/fill` and inspect current workflow state. Lever prepares an answer plan before its approved browser run. Greenhouse also fills the visible form for local review: outbound candidate traffic is blocked and files are staged inside Playwright. Staging is not an employer upload or receipt. A saved Greenhouse handoff can retry filling when no submission attempt exists. Reuse confirmed answers rather than asking for them again.

Both senders require an enabled deployment and matching per-application approval. Greenhouse opens a fresh sending browser and records its attempt before the native widget uploads a file. Keep documents without matching employer upload fields out of the attachment selection, and explain their omission. Real sending remains disabled during development. Read [employer form adapters](../../../docs/EMPLOYER_FORMS.md) for supported controls, transport validation, and recovery limits. Other portals and unsupported controls retain the normal-browser handoff.

The answer planner uses the resume writer's confirmed career context, then retrieves relevant workspace sources when factual evidence is missing. A separate review checks proposed answers; neither stage creates confirmed career facts. Use the guided question controls for missing facts and material commitments. Questions about interests, motives, values, or aspirations need confirmed personal context. If it is absent, flag the question for human intervention and leave the answer blank. Show relevant career evidence separately; do not invent a motivation to complete a mixed question. Drafts based on known personal context still need the candidate's review. A user answer resumes the same workflow. Required form qualifications queue a new fit assessment; code still enforces the recommendation gates. Pause for verification or CAPTCHA in the visible browser. Keep codes ephemeral. Continue, Save, and signed attestations can be terminal actions and must remain guarded.

Output: filled-form evidence, questions, or a normal-browser handoff. Stop on changed form structure, a rejected controlled browser, unsupported widgets, or a new required qualification. Never infer submission from a button click.
