# Contributing

Thanks for your interest in contributing! This project is developed in the
open, in the same spirit as [CARE](https://github.com/ohcnetwork/care), which
we use as an architectural reference for healthcare-grade Django backends.

## Setting up the development environment

```bash
cp .env.example .env      # fill in local values
make build
make migrate
make up
```

The API will be available at `http://localhost:8000/api/v1/`, with interactive
docs at `http://localhost:8000/api/docs/`.

Install pre-commit hooks so lint/format run automatically on commit:

```bash
pipenv install --dev
pipenv run pre-commit install
```

## Project structure

See [ARCHITECTURE.md](ARCHITECTURE.md) for how `apps/doctors` and
`apps/patients` are kept modular within this single repo, and
[ROADMAP.md](ROADMAP.md) for what's planned vs. built.

## Branching & commits

- Branch off `develop`: `issues/{issue#}/{short-description}`
- Keep commits focused; write commit messages that explain *why*, not just *what*
- Open PRs against `develop` using the PR template

## Before opening a PR

```bash
ruff check . --fix
ruff format .
pytest
python manage.py makemigrations --check --dry-run   # no missing migrations
```

## Code style

- Ruff is the linter/formatter (replaces black/isort/flake8) — config in `pyproject.toml`
- One Django app per bounded context; cross-cutting code goes in `apps.core`
- Use DRF serializers for all request/response validation — don't hand-roll JSON parsing
- Every model inherits `apps.core.models.BaseModel` (soft-delete, audit timestamps, external UUID)
- Write tests alongside the code you add (`apps/<app>/tests/`)

## Reporting bugs / requesting features

Use the issue templates. For security vulnerabilities, see
[SECURITY.md](SECURITY.md) instead of opening a public issue.

## Good first issues

Look for issues labeled `good first issue` or `help wanted`.
