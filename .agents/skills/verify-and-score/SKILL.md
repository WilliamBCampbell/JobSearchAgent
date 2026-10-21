---
name: verify-and-score
description: Verify an official opening and score it under the correct engineering or solutions rubric.
---

# Verify and score

Inputs: employer, role, requisition, discovery sources, confirmed candidate facts, and official route.

Verify the live official route through the verification service. A redirect, missing requisition, closed page, or absent application route requires review. Keep compensation components separate and distinguish confirmed Ohio eligibility from unspecified state eligibility.

Use `Assessment` and `Requirement` contracts in `src/jobsearch/scoring.py`. Every category needs sourced requirements, candidate evidence, an assessment, and deductions. The engineering weights are 35/25/15/10/10/5; solutions engineering uses its own customer-facing rubric. Code enforces central qualification gaps, technical and seniority minima, and at most five primary recommendations. Two material unconfirmed requirements remain conditional.

Output: score, matrix, deductions, source capture, and conditional or excluded reasons. Reevaluate when the form adds a requirement. Stop at central required gaps; arithmetic cannot waive them.
