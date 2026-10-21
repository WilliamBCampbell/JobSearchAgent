# Security and privacy

This is a local, single-user application. Keep it bound to localhost. Public source availability does not make the API suitable for untrusted network access or multi-user hosting.

Keep career documents, original trackers, private state, access tokens, database connection files, browser profiles, model packets, and backups outside the repository. `.env.local` is ignored and may hold local environment overrides. The tracked `.env.example` contains placeholders only. Codex login credentials stay in the Codex-managed credential store; do not copy them into this project.

Do not attach raw logs, database dumps, screenshots, browser traces, application records, or configuration files to public issues. Reproduce problems with the supplied synthetic fixtures. Redact personal paths, emails, phone numbers, authorization headers, cookies, verification codes, and document contents before sharing diagnostic material.

For a security vulnerability, use GitHub's private vulnerability reporting feature on this repository when available. Do not put working credentials or personal application data into a public report. If a credential was exposed, revoke or rotate it with its issuer; removing it from Git is not enough.

See [publication checks](docs/PUBLICATION.md) for the local hook and CI scans. Automated scanners can miss personal information or credentials, so review the exact staged diff before publishing.
