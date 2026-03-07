# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Hackathon project built on [SurrealDB](https://surrealdb.com/) — a multi-model database (relational, document, graph, time-series). Tech stack, frontend, and backend are not yet chosen. The repository is currently empty aside from this file.

## Local SurrealDB

```bash
docker run --rm -p 8000:8000 surrealdb/surrealdb:latest start --log debug --user root --pass root memory
```

## SurrealDB Conventions

- Use environment variables for connection config (`SURREALDB_URL`, `SURREALDB_NS`, `SURREALDB_DB`, `SURREALDB_USER`, `SURREALDB_PASS`). Never hardcode credentials.
- Prefer schema-full mode (`DEFINE TABLE`, `DEFINE FIELD`, `DEFINE INDEX`).
- Use `RELATE` for graph edges with descriptive verb names (`->purchased->`, `->follows->`).
- Keep complex queries in `.surql` files under `src/db/queries/`.
- Record IDs use `table:id` format (e.g., `user:alice`). Preserve this across API boundaries.
- Use parameterized queries; never interpolate raw user input into SurrealQL.
- Wrap multi-statement writes in `BEGIN TRANSACTION; ... COMMIT TRANSACTION;`.
- Use `DEFINE SCOPE` / `DEFINE TOKEN` for auth rather than root credentials in the app.

## Working in This Repo

- **Schema first**: define SurrealDB schema (`.surql` or migration) before writing application code that depends on it.
- Update this file when new tools, patterns, or conventions are established.
- All changes go on a feature branch; never push directly to `main`.
