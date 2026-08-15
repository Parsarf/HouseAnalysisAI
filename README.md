# ACQ

Foundation scaffold for the Property Acquisition Analysis Platform.

The implementation is divided into independent work-package directories. Shared contracts live in `contracts/`; packages must not import one another's implementation modules. See [acq-build-packages.md](acq-build-packages.md) for ownership, dependencies, acceptance criteria, and integration gates.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
make test
```

The current baseline contains the frozen contract layer, common primitives, development queue, auth service, API health endpoint, ownership map, and fixture layout. Production database models, extraction, numeric engines, and UI are separate package lanes and must be implemented against these seams.
