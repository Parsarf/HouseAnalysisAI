# Contribution rules

- Work in one `wp-*` branch/worktree per package.
- Only `contracts/`, `common/`, and documented public interfaces may cross package boundaries.
- Money is `Decimal` in Python, `numeric(14,2)` in Postgres, and string-serialized in JSON.
- Missing values are `null` with a `null_reason`; never use sentinel zeroes.
- Finance, strategy, and scoring code must be deterministic and free of I/O.
- LLM calls are recorded/replayed in tests; CI never calls a live model.
- Each package owns its tables and its Alembic revision range in `db/OWNERSHIP.md`.
- Integration tests belong in `tests/integration`; package tests stay with the package.
