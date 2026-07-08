# Architecture

## Why one repo, two apps

The doctor and patient experiences are distinct products with different auth
flows, permissions, and UI clients — but they share the same clinical data
(encounters, prescriptions, records) and the same underlying auth/audit
infrastructure. Splitting into two repos would mean duplicating that shared
layer or standing up an internal API between them for no real benefit at this
stage. A single repo with two modular Django apps (`apps.doctors`,
`apps.patients`) gets clean separation of concerns without that duplication.
This can still be split into separate services later if scale demands it —
the app boundaries are drawn so that split stays cheap.

## App boundaries

| App | Owns | Depends on |
|---|---|---|
| `apps.core` | `BaseModel` (soft delete, audit timestamps, external UUID), health check, any cross-cutting utility | nothing domain-specific |
| `apps.users` | The single `User` auth model (shared by both journeys via `user_type`), JWT auth endpoints | `apps.core` |
| `apps.doctors` | Everything in the doctor journey: clinic/facility onboarding, scheduling, encounters, prescriptions, inventory, billing, analytics | `apps.core`, `apps.users` |
| `apps.patients` | Everything in the patient journey: unified profile, consent/record-sharing, patient-facing history views | `apps.core`, `apps.users` |

`apps.doctors` and `apps.patients` must never import from each other directly.
Anything both sides need (e.g. a shared `Encounter` or `Prescription` record
a patient views read-only and a doctor edits) should be modeled once — the
current plan is to introduce a third shared domain app (`apps.clinical` or
similar, FHIR-resource-style) once that need is concrete, rather than
guessing its shape now. See [ROADMAP.md](ROADMAP.md).

## Auth model

One `User` model (`apps.users.models.User`) with a `user_type` discriminator
(`doctor`, `nurse`, `receptionist`, `lab_technician`, `pharmacist`,
`clinic_admin`, `patient`). Each side extends it with a one-to-one profile:

- `apps.doctors.models.DoctorProfile`
- `apps.patients.models.PatientProfile`

This keeps a slim core `User` separate from richer per-context profile data,
and keeps `AUTH_USER_MODEL` swap-free for the life of the project (changing
it after the first migration is a painful Django migration, so it's set
correctly from commit one).

Doctor-side login is expected to be email/password (+ optional SSO later);
patient-side login is expected to be phone/OTP-based. Both issue the same
JWT shape so downstream permission checks don't need to branch on which flow
was used — only on `user_type`.

## Settings

Environment split:

- `config/settings/base.py` — shared defaults
- `config/settings/local.py` — dev overrides (`DJANGO_SETTINGS_MODULE=config.settings.local`)
- `config/settings/test.py` — used by pytest
- `config/settings/deployment.py` — production hardening (HSTS, secure cookies, etc.)

## Data model conventions

Every model inherits `apps.core.models.BaseModel`:

- `external_id` (UUID) — the ID ever exposed over the API; internal `id` stays a sequential PK
- `created_date` / `modified_date` — audit trail
- `deleted` — soft delete; nothing is ever hard-deleted from clinical data

## What's deliberately not decided yet

The full doctor/patient journeys (see ROADMAP.md) span scheduling, EMR,
inventory, billing, AI notetaking, consent management, and audit logging.
Rather than pre-designing all of those schemas now, each will get its own
architecture note and migration set when its epic starts — locking in
`FHIR`-style resource shapes too early, before we know exactly which fields
the AI/voice layer needs to write to, would cost more in rework than it saves.
