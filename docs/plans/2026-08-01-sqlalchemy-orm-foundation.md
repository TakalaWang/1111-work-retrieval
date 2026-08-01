# SQLAlchemy ORM Foundation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make SQLAlchemy 2.0 the explicit owner of future PostgreSQL domain models without creating any domain tables yet.

**Architecture:** Add one Python workspace package that exports an empty typed `DeclarativeBase`. Alembic consumes that exact metadata as its autogenerate source of truth. API request/response models remain separate Pydantic types, and database connection/session lifecycle waits until the first real persistence use case.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 ORM, Alembic, psycopg 3, PostgreSQL 16, uv.

---

### Task 1: Establish ORM metadata ownership

**Files:**

- Create: `packages/database/pyproject.toml`
- Create: `packages/database/src/work_retrieval_database/__init__.py`
- Create: `packages/database/src/work_retrieval_database/models.py`
- Create: `packages/database/tests/test_models.py`
- Modify: `pyproject.toml`
- Modify: `database/env.py`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `.kiro/specs/job-search-api-platform/{requirements,design,tasks}.md`
- Regenerate: `uv.lock`

**Step 1: Write the failing test**

Add a test importing `Base` and asserting that its metadata has no tables. Run only that test and confirm it fails because the database package does not exist.

**Step 2: Implement the minimum ORM package**

Create a typed SQLAlchemy 2.0 `DeclarativeBase`, export it, declare SQLAlchemy as the package dependency, and add the package to the uv workspace. Do not add models, repositories, session factories, engines, or compatibility helpers.

**Step 3: Wire Alembic to ORM metadata**

Set `target_metadata = Base.metadata` in `database/env.py`. Keep PostgreSQL-only URL validation and the empty baseline unchanged.

**Step 4: Document the ownership boundary**

State that SQLAlchemy owns PostgreSQL domain models, Alembic owns schema changes, Pydantic owns HTTP contracts, and the scaffold intentionally has no domain tables or runtime session lifecycle yet.

**Step 5: Verify**

Run the focused test, full Python tests, Ruff, strict mypy, OpenAPI drift, PostgreSQL 16 migration twice with zero domain tables, frozen installs, Node checks/build, actionlint, and CDK synth. Confirm generated files do not drift.

**Step 6: Commit**

Commit the implementation as `feat: establish SQLAlchemy ORM foundation`.
