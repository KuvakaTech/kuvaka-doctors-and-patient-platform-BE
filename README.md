# Kuvaka Platform Backend

An open-source backend serving two connected clinical journeys — the
**doctor/clinic platform** and the **patient platform** — as one Django
project with clearly separated, modular apps.

The architecture favors conventions well-suited to healthcare-grade Django
backends: FHIR-ish resource modeling, audit-friendly base models, DRF + JWT,
and environment-split settings.

## Status

Early scaffolding stage — the project skeleton, auth model, and modular app
boundaries are in place. See [ROADMAP.md](ROADMAP.md) for what's built vs.
planned, and [ARCHITECTURE.md](ARCHITECTURE.md) for how the codebase is
organized.

## Stack

- **Django 5** + **Django REST Framework** — API layer
- **PostgreSQL** — primary datastore
- **Redis** — caching (Celery task queue to follow as async workloads land)
- **SimpleJWT** — authentication
- **drf-spectacular** — OpenAPI schema + Swagger UI at `/api/docs/`
- **Ruff** — linting/formatting
- **pytest-django** — testing

## Quickstart

```bash
git clone <this repo>
cd kuvaka-doctors-and-patient-platform-BE
cp .env.example .env
make build
make migrate
make up
```

API: `http://localhost:8000/api/v1/`
Docs: `http://localhost:8000/api/docs/`
Admin: `http://localhost:8000/admin/`

Without Docker:

```bash
pipenv install --dev
pipenv shell
export DJANGO_READ_DOT_ENV_FILE=true
python manage.py migrate
python manage.py runserver
```

## Repository layout

```
config/                 # Django settings (base/local/test/deployment), root URLs
apps/
  core/                 # Shared BaseModel, health check — no domain logic
  users/                # Single auth model (User) shared by both journeys
  doctors/              # Doctor/clinic-facing domain (modular, owns its own models/serializers/views)
  patients/             # Patient-facing domain (modular, owns its own models/serializers/views)
```

## Tooling

- **`.env.example` / `.env`** — `django-environ` reads `.env` when
  `DJANGO_READ_DOT_ENV_FILE=true`, and it's also loaded into the `backend`
  container by `docker-compose.yaml`. Copy it to `.env` and fill in real
  values before running anything; `.env` itself is gitignored.
- **`Dockerfile`** — builds one image via `pipenv install --system`. Its
  default command runs `gunicorn` (production-oriented), but
  `docker-compose.yaml` overrides that with `manage.py runserver` and a
  live-reload volume mount for local development.
- **`docker-compose.yaml`** — runs `db` (Postgres), `redis`, and `backend`
  together for local dev.
- **`Makefile`** — short verbs over `docker compose`: `make build`/`up`/`down`
  manage the stack, `make migrate`/`makemigrations`/`shell`/`test` run one-off
  Django commands inside a throwaway container. `make lint`/`format` run
  `ruff` directly on the host instead (no Docker needed for those two).
- **`pyproject.toml`** — tool config only (no packaging metadata): Ruff
  lint/format rules and pytest settings (`config.settings.test`, `--reuse-db`
  for faster repeat runs).
- **`.pre-commit-config.yaml`** — if installed
  (`pipenv run pre-commit install`), runs Ruff lint/format plus basic hygiene
  checks (trailing whitespace, YAML validity, merge conflict markers) on every
  commit, before code reaches CI.
- **`.github/workflows/ci.yml`** — runs on every push/PR to `main`/`develop`,
  independent of Docker: a `lint` job (`ruff check` + `ruff format --check`)
  and a `test` job that spins up real Postgres/Redis service containers,
  migrates, and runs `pytest --cov=apps`.
- **`.github/dependabot.yml`** — weekly automated PRs bumping pip, Docker
  base image, and GitHub Actions dependencies.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
For vulnerabilities, see [SECURITY.md](SECURITY.md) instead of filing a public issue.

## License

MIT — see [LICENSE](LICENSE).
