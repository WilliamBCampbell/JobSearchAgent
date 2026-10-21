# Application documents

The earlier renderer printed selected memory values as paragraphs. It could omit employment history, repeat overlapping facts, and expose confirmation notes. Cover letters used the first three selected facts. Selecting relevant evidence did not produce a finished document.

The renderer converts structured, evidence-backed content from HTML to DOCX and then to US Letter PDF. Original document wording and layout references do not establish current career facts.

## Modules and controls

| Stage | Responsibility |
|---|---|
| Structured memory | Current scoped career facts, confirmed employment history, and training. Conflicts stop preparation. Contact fields stay out of the model packet. |
| `package_draft` | Write a summary, grouped skills, employer-specific bullets, education, and separate cover-letter paragraphs. Each claim cites confirmed facts. |
| `package_review` | Check the exact draft for factual support, employer attribution, ownership, tenure, relevance, repetition, and prose quality. Report specific issues. |
| Content validation | Require all structured employment records, valid references, supported numeric claims, and no duplicate passages or internal confirmation notes. |
| HTML renderer | Supply exact employment titles and dates, contacts, and document structure. Escape all content. |
| Conversion and checks | Convert HTML to DOCX, patch US Letter dimensions, convert to PDF, check one-page layout and text equality, inspect page-edge positions, and render a page image. |
| Package service | Publish complete versions atomically, retain hashes and per-document claim references, ingest generated text as drafts, and expose current previews separately from older versions. |

The writing and review routes are configurable in model settings. Defaults use the existing Codex provider, with higher reasoning effort for writing than for the separate review. They use the same enabled data destination, bounded subprocess runner, cancellation, event monitoring, and evidence-backed cache as other model tasks. The fixture backend produces deterministic documents without hosted calls. No model response changes career memory or grants submission authorization.

## Writing instructions

Drafting and evidence/prose review load the complete installed `no-ai-slop/SKILL.md` and its `references/ai-writing-detection.md`. Both files are included in the Codex prompt because these workers cannot discover skills or read files. The tasks require the full self-check. Review issues identify the passage, the evidence gap or writing rule, and the correction needed. Unresolved prose issues stop publication through the existing review and revision process.

Cover letters use the user's requested cordial tone. The writer opens with interest in the position and a reason grounded in the posting and confirmed experience, then develops relevant examples. Review checks this opening and allows ordinary courtesy without inventing personal motivations. The template adds thanks for the reader's time and an invitation to discuss the team's needs and how the candidate could contribute. The generated body leaves that closing to the template so it appears once. Resumes retain their concise, factual style.

The default source is `~/.agents/skills/no-ai-slop/SKILL.md`. If it is absent, the loader checks sibling repositories: `no-ai-slop/skills/no-ai-slop/SKILL.md`, `no-ai-slop/SKILL.md`, then `no_ai_slop_writing_rules/skills/no-ai-slop/SKILL.md`. Set `JSA_NO_AI_SLOP_SKILL` to an absolute `SKILL.md` path to choose a source explicitly. Keep its `references` directory beside it. For example:

```bash
export JSA_NO_AI_SLOP_SKILL="$HOME/.agents/skills/no-ai-slop/SKILL.md"
.venv/bin/python scripts/services.py start
```

Restart existing services when changing that environment variable. Edits to either instruction file are read on each task and invalidate cached drafting and review results. Missing, empty, or unreadable files stop the task; an explicit path never silently falls back. If instructions change during a model run, its result is rejected. Private inference events record the loaded paths and content hashes. A new review creates a distinct package version even when the document text is unchanged, preserving the earlier review record.

Skill examples are style guidance, not career evidence. Confirmed names and titles, exact quotations, and necessary uncertainty retain their meaning. These prompt instructions supplement factual validation; they do not guarantee that a model detects every writing problem. Offline fixtures exercise instruction delivery and review controls without performing a semantic prose review. Existing packages remain unchanged until preparation is requested again.

One preparation attempt allows one writing revision for content or layout feedback. Successful inference results can be reused after a worker restart. A failed review or incomplete rendering does not publish attachments. A changed existing package hash is an integrity error, not a reason to overwrite it. Application state and approved attachment versions remain controlled by the existing workflow service.

## Rendering and review

### User-requested revisions

The application page has a **Review your application documents** step beside the previews. Enter application-specific feedback under **What would you like changed?**, then choose **Regenerate with my suggestions**. The request records the exact package version and prior structured draft, with up to 2,000 characters of notes. The writer and evidence/prose reviewer both receive them. Automatic repair feedback supplements the user's notes. New career claims still require confirmed facts; neither the notes nor the previous draft become career evidence.

Revision requests use the durable package queue. Repeated clicks reuse the same active request. A changed source version, modified file, active application task, imported record, or existing submission attempt blocks the request with an explanation. Requesting a revision cancels queued application work and invalidates existing submission approval and browser review. The old files remain available while generation runs. Successful generation replaces selected attachments with the corresponding formats from the new package; a prepared but unattached cover letter stays unattached. Earlier files and notes remain available for inspection.

Choose **Documents look good** after inspecting the new previews. This records review of that document version only. It grants no submission authority and does not replace final review of employer answers, selected files, and commitments. The journey points to document review before continuing with answers. Agents may still prepare answers before that review; final submission continues to require its existing explicit approval.

Failed or cancelled revisions preserve the previous package. **Revision requests** retains the notes and task result, with **Edit these suggestions** to prepare another request. A worker restart after publication recognizes the completed revision rather than generating another version. The fixture model does not interpret arbitrary editorial requests; fixture tests use controlled revised responses to verify delivery, rendering, versioning, and review controls. Live revision quality depends on the configured writer and reviewer.

On Debian or Ubuntu, install the system rendering tools with `sudo apt-get install libreoffice-writer poppler-utils fonts-liberation2`. The service checks for LibreOffice, `pdfinfo`, `pdftotext`, and `pdftoppm` before starting a paid drafting task. Python dependencies remain in the repository lockfile. The PDF producer version is recorded in the document checks.

The conversion uses LibreOffice's `docx:Office Open XML Text` and `pdf:writer_pdf_Export` filters. [LibreOffice's filter documentation](https://help.libreoffice.org/latest/en-US/text/shared/guide/convertfilters.html) describes these names and command syntax. The existing Codex runner uses a strict output schema for each task, consistent with the [official structured-evaluation guidance](https://developers.openai.com/blog/eval-skills).

`Package_Manifest.json` records file hashes and rendering checks. `Document_Content.json` contains the structured draft. `Document_Review.json` preserves claim references, inference run IDs, and review results. `Application_Review.html` displays the PDF page images. All files live in private version directories outside Git.

Versioned packages show only the files and page images belonging to the version under review. A newer artifact left by an unpublished attempt cannot replace those previews. Older applications without a version use the newest file for each document and format, ordered by file modification time with package membership breaking ties. Older versions remain collapsed under **Previous document versions**, with existing attachment selections identified. Filtering the display does not change attachments or delete files. PDF text must match the source HTML, and each page must measure 612 × 792 points. Text extending into the page edge stops preparation. Short pages produce a spacing warning. The template keeps resume body text at 9.6 points and cover-letter text at 11 points; overflow requires content revision rather than automatic font shrinking.

Automated checks and a model review do not establish perfect factual accuracy or visual approval. Inspect page images for wrapping, spacing, and heading placement. The recorded `visual_review` remains pending until a separate review records its outcome; generation does not claim that a person inspected the page. Optional missing details are omitted rather than converted into unsupported claims or unnecessary questions.

## Validation

`tests/test_documents.py` exercises history retention, provenance, internal-note rejection, duplicate text, unsupported numbers, conflicting facts, HTML escaping, PDF/DOCX dimensions, immutable reuse, cancellation, and rejection before artifact publication. The browser and employer-form suites exercise generated packages through attachment selection and guarded fixture submissions.
