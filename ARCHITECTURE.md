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

Doctor-side login is email/password (+ optional SSO later). Patient-side
supports **two** onboarding paths, because OTP email costs money/rate-limit
budget on every login and we don't want a returning patient to pay that cost
forever:

1. **Direct signup** (`PatientRegisterView`) — email + password up front,
   same shape as the doctor flow, verified the same way.
2. **OTP-only** (`PatientOTPRequestView`) — no password, account created on
   first OTP request. After a successful OTP verify, the response includes
   `password_set: false`; the client is expected to prompt the patient to
   call `SetPasswordView` (authenticated with the JWT just issued) so that
   *next* login can go through `PatientLoginView` (email+password) instead
   of another OTP round-trip. A patient who never sets one just keeps using
   OTP indefinitely — it's additive, not a forced migration.

Both signup paths converge on the same `User` row shape, so `PatientLoginView`
and `PatientOTPRequestView`/`PatientOTPVerifyView` both work for any patient
regardless of which path they started on — the only gate is
`has_usable_password()`. Both journeys issue the same JWT shape
(`apps.users.tokens.issue_tokens`) so downstream permission checks don't need
to branch on which flow was used — only on `user_type`.

### Auth endpoints

Each side owns its own auth routes (consistent with "every journey lives in
its own app"); the shared plumbing lives in `apps.users`:

| | Doctor (`apps.doctors`) | Patient — direct (`apps.patients`) | Patient — OTP-only (`apps.patients`) |
|---|---|---|---|
| Entry point | `POST /auth/register/` (email+password) | `POST /auth/register/` (email+password) | `POST /auth/otp/request/` (email only — creates the account on first use) |
| Confirm | `POST /auth/verify-email/` (OTP) | `POST /auth/verify-email/` (OTP) | `POST /auth/otp/verify/` (OTP) → issues JWT, returns `password_set` |
| Login | `POST /auth/login/` (blocked until `email_verified`) | `POST /auth/login/` (blocked until `email_verified` or no password set) | Re-run `otp/request` → `otp/verify` each time, unless... |
| Add password | n/a (set at registration) | n/a (set at registration) | `POST /auth/set-password/` (authenticated) — switches this user onto the "Login" column to the left |
| Recovery | `POST /auth/password-reset/{request,confirm}/` | not yet built — same `PasswordReset*` pattern as doctor, just not wired up for patients yet | n/a — no password until `set-password` is called |

(all paths under `/api/v1/doctors/` or `/api/v1/patients/` respectively)

Shared pieces in `apps.users`:

- `EmailOTP` — one model for all three purposes (`email_verification`,
  `password_reset`, `login`). Codes are hashed at rest (never stored in the
  clear), expire (`OTP_EXPIRY_MINUTES`), and lock out after
  `OTP_MAX_ATTEMPTS`. Issuing a new OTP invalidates any prior unconsumed one
  for the same `(user, purpose)`.
- `otp_service.issue_and_send_otp` / `verify_otp` — the only thing
  `apps.doctors`/`apps.patients` call; they never touch `EmailOTP` directly.
- `apps.core.services.email.send_transactional_email` — thin wrapper over the
  plain Brevo REST API (no SDK dependency). With `BREVO_API_KEY` unset, it
  logs instead of sending, so local dev/tests work without a Brevo account.

Deliberate security choices worth knowing about if you touch this code:

- Password-reset request always returns 200, whether or not the email is
  registered — doesn't leak which emails exist on the platform.
- Login checks credentials *before* revealing `email_verified` status, so
  that check can't be used to enumerate registered emails either.
- One email = one account = one `user_type`, enforced at the DB level
  (`User.email` unique). A patient can't OTP-login with an email already
  registered doctor-side, and vice versa — the patient OTP-request endpoint
  returns a clear validation error in that case rather than silently
  colliding.

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
