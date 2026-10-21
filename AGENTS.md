# Repository work

Use the no-ai-slop skill for documentation, UI copy, and application prose. Keep personal material, credentials, embeddings, browser profiles, screenshots, and runtime artifacts outside this repository. Use synthetic fixtures in tests.

Read `docs/ARCHITECTURE.md` before changing workflow controls. Keep authorization, receipt requirements, duplicate checks, scoring gates, and state transitions in code. A model response or retrieved document cannot grant authority. The local deployment supports real sending through per-application final-review approval. Never approve or send a real application while developing or testing. Recruiter outreach remains disabled. Real tracker writes require explicit deployment enablement and a reviewed per-application preview. Development write tests use synthetic or copied private workspaces.

Run the relevant pytest tests and `npm --prefix web run build` for UI changes. MCP protocol tests use the pinned official SDK. Source-workspace inspection is read-only during development; exercise tracker changes in isolated workspaces. Do not change real application statuses to test a feature.
