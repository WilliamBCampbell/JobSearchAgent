---
name: prepare-package
description: Prepare resumes, cover letters, and narratives from confirmed evidence for a selected application.
---

# Prepare package

Inputs: selected application, current confirmed facts, official requirements, and requested document formats.

Use the package service through `POST /api/applications/{id}/prepare`. Review PDF, DOCX, and HTML output and the claim-to-fact references. Separate writing and review tasks produce and check a structured document through the configured inference service. The offline provider supplies a deterministic fixture. Neither provider can confirm new career claims. Regenerate when evidence changes.

The renderer follows the workspace process: structured HTML, LibreOffice DOCX conversion, US Letter page dimensions, then PDF conversion. It checks page count, extracted text against the HTML, and page-edge positions, and generates a page image with `pdftoppm`. Inspect the actual PDF preview for spacing, contact wrapping, and heading placement. Automated text and geometry checks do not count as visual approval. Never reduce body text to hide overflow; revise the document.

Apply the installed no-ai-slop skill to human-facing prose and run its self-check. Preserve title, employment dates, degree status, ownership, metrics, and production tenure exactly as confirmed. Contributing to a shared framework does not mean being its primary developer. Cover letters should open with interest in the role and a reason grounded in the posting and confirmed experience, then develop relevant examples. The template supplies thanks and an invitation to discuss the employer's needs. This cordial tone is requested by the user and should survive prose review. Ask for personal narrative details only when the application requires them and the evidence cannot answer; do not ask the user to reconfirm ordinary interest in applying.

Output: versioned package files, hashes, answer records, and a readable review. Choose only the files actually attached. Stop if a claim lacks current confirmation or a rendered PDF fails extraction. Submitted versions are immutable.
