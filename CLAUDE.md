# CLAUDE.md — SurrealDB Hackathon Project

This file provides guidance for AI assistants (Claude Code and similar) working in this repository. Update this file as the project evolves.

---

## Project Overview

This is a hackathon project built on [SurrealDB](https://surrealdb.com/), a multi-model database supporting relational, document, graph, and time-series data in a single engine. The repository is currently in initial setup — update this section once a project direction is chosen.

---

## Repository State

> **Note:** This repository was bootstrapped empty. Update all sections below as files and structure are added.

---

## Tech Stack (fill in when decided)

| Layer        | Technology          |
|--------------|---------------------|
| Database     | SurrealDB           |
| Backend      | TBD (Rust / Node.js / Python / Go) |
| Frontend     | TBD                 |
| Auth         | SurrealDB built-in / TBD |
| Deployment   | TBD                 |

---

## Project Structure (update as files are added)

```
surrealdb-hackathon/
├── CLAUDE.md              # This file
├── README.md              # Human-readable project overview
├── .env.example           # Environment variable template (never commit .env)
├── docker-compose.yml     # Local SurrealDB + services
├── src/                   # Application source code
│   ├── db/                # SurrealDB connection, queries, migrations
│   └── ...
└── tests/                 # Test suite
```

---

## SurrealDB Conventions

### Connection

- Always use environment variables for the SurrealDB endpoint, namespace, database, username, and password.
- Required env vars:
  ```
  SURREALDB_URL=ws://localhost:8000/rpc   # or http://
  SURREALDB_NS=hackathon
  SURREALDB_DB=main
  SURREALDB_USER=root
  SURREALDB_PASS=root
  ```
- Never hardcode credentials. Use `.env` locally and secret managers in production.

### SurrealQL Style

- Use `DEFINE TABLE`, `DEFINE FIELD`, and `DEFINE INDEX` statements to enforce schema (schema-full mode preferred over schema-less for production).
- Use `RELATE` for graph edges; name edges with descriptive verbs (`->purchased->`, `->follows->`).
- Keep complex queries in dedicated `.surql` files under `src/db/queries/` rather than embedding long strings in application code.
- Prefer `SELECT` with explicit field lists over `SELECT *` for clarity and performance.
- Use `RETURN AFTER` or `RETURN BEFORE` in `UPDATE`/`DELETE` when the caller needs the record.
- Always scope permissions with `DEFINE SCOPE` / `DEFINE TOKEN` rather than using root credentials in the app.

### Record IDs

- SurrealDB record IDs have the form `table:id` (e.g., `user:alice`). Preserve this structure when passing IDs across API boundaries.
- Use custom string IDs (`user:⟨alice@example.com⟩`) or `rand()` / `ulid()` / `uuid()` depending on requirements.

### Transactions

- Wrap multi-statement writes in `BEGIN TRANSACTION; ...; COMMIT TRANSACTION;` to maintain consistency.

---

## Development Workflow

### Local Setup

1. Start SurrealDB locally (Docker recommended):
   ```bash
   docker run --rm -p 8000:8000 surrealdb/surrealdb:latest start \
     --log debug --user root --pass root memory
   ```
   Or use `docker-compose up` if a `docker-compose.yml` exists.

2. Copy environment variables:
   ```bash
   cp .env.example .env
   # Edit .env with your local values
   ```

3. Install dependencies and start the app (commands TBD once stack is chosen).

### Running Tests

- Document test commands here once a testing framework is set up.
- Prefer running a dedicated in-memory SurrealDB instance for tests (not the dev instance).
- Tests must not rely on data left over from previous runs; always set up and tear down test fixtures.

### Making Changes

1. Branch from `main` using the convention `<username>/<short-description>`.
2. Keep commits small and focused; use the imperative mood in commit messages (`Add user schema`, `Fix login query`).
3. Run linting and tests before pushing.
4. Open a pull request and request review before merging.

---

## Security Guidelines

- Never commit `.env`, secrets, or credentials. Add `.env` to `.gitignore` immediately.
- Use SurrealDB scopes and permissions (`DEFINE SCOPE`, `DEFINE TABLE ... PERMISSIONS`) to enforce row-level access control rather than relying solely on application-layer checks.
- Validate and sanitize all user input before interpolating it into SurrealQL strings. Prefer parameterized queries / SDK-provided query builders.
- Keep SurrealDB versions pinned and audit dependencies regularly.

---

## AI Assistant Instructions

When working in this repository:

1. **Read before writing.** Always read existing files before editing them.
2. **Minimal changes.** Only modify what is needed; do not refactor unrelated code.
3. **Schema first.** When adding data models, define the SurrealDB schema (`.surql` or migration file) before writing application code that depends on it.
4. **Environment variables.** Never hardcode URLs, credentials, or environment-specific values.
5. **Keep this file updated.** When new tools, patterns, or conventions are established, update the relevant section of CLAUDE.md.
6. **Test coverage.** Add or update tests when changing business logic or database queries.
7. **Commit on branch.** All changes go on a feature branch; never push directly to `main`.
8. **No speculative work.** Do not add features, helpers, or abstractions that are not explicitly required by the current task.

---

## Useful SurrealDB Resources

- [SurrealDB Docs](https://surrealdb.com/docs)
- [SurrealQL Reference](https://surrealdb.com/docs/surrealql)
- [SurrealDB SDKs](https://surrealdb.com/docs/sdk) (Rust, JavaScript, Python, Go, Java, .NET)
- [SurrealDB GitHub](https://github.com/surrealdb/surrealdb)
