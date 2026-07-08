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

## Doctor journey

### 1. Authentication & Dashboard
- [ ] Email + password login (JWT) — model supports it, endpoint pending
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
- [ ] Questionnaire-based documentation (SOAP / progress / consultation notes) — see CARE's questionnaire engine as reference pattern

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

- [ ] Auth layer (phone/OTP)
- [ ] Unified profile & its management
- [ ] Analytics & dashboard (patient-facing)
- [ ] Consent management: grant/revoke access, share records, share temporarily
- [ ] Doctor-visible-on-consent data: previous visits, diagnoses, allergies, current/past medications, surgeries, chronic diseases, family history, vaccinations, lab/imaging reports, discharge summaries, previous prescriptions, AI summaries, transcripts, recordings, uploaded documents, previous referrals

## Sequencing notes

- RBAC and audit logging are called out as "very important for healthcare" —
  build these alongside the first real clinical data model, not after.
- The AI intelligence layer (notetaker, transcription, SOAP generation,
  suggestions) is scoped as its own workstream once the core patient/encounter
  data model exists to write into — see `care_scribe` in the CARE ecosystem
  for how a comparable AI layer is kept as an add-on rather than baked into
  core models.
- Consent management (patient journey) gates "Patient History" (doctor
  journey item 4) — build patient-side grant/revoke before doctor-side reads
  depend on it.
