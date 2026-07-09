# Roadmap

Tracks the full scope discussed for both journeys, against what's actually
built. Check items move to "Done" only when merged to `main`, not when
started — use GitHub issues (one per unchecked item, or per logical group)
for in-progress tracking, linking back here.

## Done

- [x] Repo scaffolding: license (MIT), CI, pre-commit, Docker, Makefile
- [x] `config/settings` split (base/local/test/deployment)
- [x] `apps.core.BaseModel` (soft delete, audit timestamps, external UUID)
- [x] `apps.users.User` — single shared auth model with `user_type`, JWT issuance
- [x] `apps.doctors` / `apps.patients` app skeletons with a profile model each
- [x] Doctor auth: register (email+password) → email verification (Brevo OTP) → login (JWT), password reset
- [x] Patient auth: passwordless email-OTP request/verify (Brevo), lazy account creation on first OTP
- [x] Shared `EmailOTP` model (hashed codes, expiry, max attempts) + `apps.core` Brevo email service

## Doctor journey

### 1. Authentication & Dashboard
- [x] Email + password login (JWT), with email verification and password reset — all via Brevo OTP emails
- [ ] Google SSO
- [ ] Doctor profile & specialties (model stub exists — `DoctorProfile.specialties`)
- [ ] Preferred medicine inventory (voice / OCR / text input)
- [ ] Dashboard: today's appointments, waiting patients, ongoing consultations, recent patients, revenue, pending reports, follow-ups due, AI insights

### 2. Hospital / Clinic Onboarding
- [ ] Register hospital/clinic, departments, consultation timings
- [ ] Staff management, roles & permissions (RBAC)

### 3. Patient Management (doctor-side view)
- [ ] Register/search patient, merge duplicates
- [ ] Family members, emergency contacts, insurance details
- [ ] Request access to a patient's unified profile
- [ ] Patient timeline across visits

### 4. Patient History (pre-consultation)
- [ ] Read unified profile data the patient has granted access to (depends on Patient journey's consent management)

### 5–6. AI Notetaker & Live Transcription
- [ ] Live transcription, speaker diarization, conversation recording (optional per-consultation toggle)

### 7. Core Clinical Workflow (AI intelligence layer)
- [ ] AI-generated summary from unified profile + transcript
- [ ] Diagnoses, symptoms, allergies, chronic conditions, treatment updates, test suggestions
- [ ] Vitals & trends (BP, HR, temp, SpO2, RR, weight, height, BMI) with visualization
- [ ] Medication management: `MedicationRequest` generation, editable prescription, stock-aware dosage/generic/alternative suggestions
- [ ] Questionnaire-based documentation (SOAP / progress / consultation notes)

### 8. Prescription Workflow
- [ ] AI-generated prescription as an editable starting point
- [ ] Voice editing, OCR editing, manual editing, multilingual output, printable PDF
- [ ] Doctor can discard AI output and author manually

### 9. Post Consultation
- [ ] Role-based actions (doctor/nurse/receptionist): upload reports/images, voice notes, OCR, clinical observations

### 10. Visit Completion
- [ ] Follow-up scheduling, discharge summary, referral, transfer, admission decision, next-consultation reminder

### Extras
- [ ] Inventory: pharmacy stock, expiry tracking, batch numbers, low-stock alerts, purchase orders
- [ ] Analytics: demographics, disease trends, revenue, prescription analytics, follow-up rate, missed appointments
- [ ] Global search across patient / visit / prescription / diagnosis / medicine / report
- [ ] Access control: doctor / nurse / receptionist / admin / lab tech / pharmacist, granular permissions
- [ ] Audit logs: who viewed/edited/deleted, timestamp, IP, device

## Patient journey

- [x] Auth layer — two onboarding paths: direct email+password signup, and passwordless email-OTP (Brevo) with optional password set-up afterward so repeat logins don't require another OTP email (phone/SMS OTP deferred until an SMS vendor is picked; see ARCHITECTURE.md)
- [ ] Patient-side password-reset request/confirm (doctor side has it; not yet wired up for patients)
- [ ] Unified profile & its management
- [ ] Analytics & dashboard (patient-facing)
- [ ] Consent management: grant/revoke access, share records, share temporarily
- [ ] Doctor-visible-on-consent data: previous visits, diagnoses, allergies, current/past medications, surgeries, chronic diseases, family history, vaccinations, lab/imaging reports, discharge summaries, previous prescriptions, AI summaries, transcripts, recordings, uploaded documents, previous referrals

## Sequencing notes

- RBAC and audit logging are called out as "very important for healthcare" —
  build these alongside the first real clinical data model, not after.
- The AI intelligence layer (notetaker, transcription, SOAP generation,
  suggestions) is scoped as its own workstream once the core patient/encounter
  data model exists to write into, kept as an add-on rather than baked into
  core models.
- Consent management (patient journey) gates "Patient History" (doctor
  journey item 4) — build patient-side grant/revoke before doctor-side reads
  depend on it.
