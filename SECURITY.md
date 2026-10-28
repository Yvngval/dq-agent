# Security

The agent can modify data, so every fix goes through three safeguards: SQL guardrails (single statement,
raw tables only, mandatory `WHERE`, at most 10% of rows), a dry run in a rolled-back transaction, and human
approval. Rows are copied to `audit.quarantine` before any change.

If you find a way to get a fix past these checks, please open a private security advisory on GitHub
rather than a public issue.
