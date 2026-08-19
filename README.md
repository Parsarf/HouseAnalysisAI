# ACQ

ACQ is a production-oriented property acquisition analysis platform: a
FastAPI/Postgres API and worker, deterministic Decimal-based underwriting,
strategy, scoring and ranking engines, whole-PDF analysis, and a React/Vite SPA.

The implementation is divided into independent work-package directories. Shared contracts live in `contracts/`; packages must not import one another's implementation modules. See [acq-build-packages.md](acq-build-packages.md) for ownership, dependencies, acceptance criteria, and integration gates.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
make test
```

The backend, worker, migrations, whole-PDF pipeline, deterministic engines, and
SPA are wired together. Use `make test`, `make lint`, `make typecheck`, and
`make web` for local gates.

## Launch prerequisites

Run `pip install -e '.[dev]'`, start Postgres with `docker compose up -d db`,
run `alembic upgrade head`, then run the validation targets. Production launch
also requires valid session, storage, database, and provider credentials plus a
reviewed anonymized-document evaluation set.
