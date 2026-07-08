# Kuvaka Platform Backend

An open-source backend serving two connected clinical journeys — the
**doctor/clinic platform** and the **patient platform** — as one Django
project with clearly separated, modular apps.

This project takes architectural inspiration from
[CARE](https://github.com/ohcnetwork/care), the open-source EMR/HMIS backend
maintained by the Open Healthcare Network. We reuse its conventions (FHIR-ish
resource modeling, audit-friendly base models, DRF + JWT, environment-split
settings) rather than reinventing them, since CARE is a well-recognized,
production-proven system in the same domain.

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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
For vulnerabilities, see [SECURITY.md](SECURITY.md) instead of filing a public issue.

## License

MIT — see [LICENSE](LICENSE).
