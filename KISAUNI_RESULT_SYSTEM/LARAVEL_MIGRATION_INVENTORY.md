# Flask → Laravel Migration Inventory (Source of Truth)

## Repository scan summary

- Flask/Python system exists under `/home/runner/work/abuudoro61/abuudoro61/KISAUNI_RESULT_SYSTEM`
- Laravel project files are currently absent (no `artisan`, `composer.json`, `app/Http`, `resources/views`, or `*.php`)

## Flask file inventory

### Core backend
- `app.py` — all routes, controllers, auth/session checks, validation, grading/workflow/report logic orchestration
- `config.py` — app config, constants, class/subject maps, grade boundaries, role constants, timezone/date helpers
- `database.py` — schema, DB initialization/lightweight migrations, seeding, backups, audit log writes/retention
- `pdf_report.py` — PDF builders for student report and class result sheet

### Templates (Jinja)
- `_macros.html`
- `base.html`
- `login.html`, `forgot_password.html`, `change_password.html`, `profile.html`
- `dashboard.html`, `headmaster_overview.html`, `analytics.html`
- `students.html`, `student_form.html`, `student_form_bulk.html`, `student_history.html`
- `classes.html`, `promote.html`, `promote_my_class.html`
- `subjects.html`, `examinations.html`
- `users.html`, `user_form.html`
- `marks_select.html`, `marks.html`
- `results.html`, `review_results.html`
- `records.html`, `reports.html`, `student_report.html`
- `settings.html`, `audit_logs.html`, `error.html`

### Static/assets
- `static/css/style.css`
- `static/js/script.js`
- `static/images/logo.png`

## Database schema and compatibility behavior

SQLite database behavior is defined in `database.py` and must be preserved.

### Tables
- `users`
- `classes`
- `subjects`
- `class_subjects`
- `students`
- `examinations`
- `marks`
- `exam_class_status`
- `settings`
- `audit_log`

### Key constraints and rules
- Role restriction check on `users.role`: `headmaster`, `class_teacher`
- Result workflow check on `exam_class_status.status`: `draft`, `submitted`, `under_review`, `returned`, `approved`
- Unique constraints:
  - `users.username`
  - `students.reg_no`
  - `examinations(exam_type, academic_year)`
  - `marks(student_id, exam_id, subject_id)`
  - `exam_class_status(exam_id, class_id)`
- Runtime SQLite pragmas in use: FK on, WAL, busy timeout

### Lightweight migration/backfill behavior currently in Flask startup
- Adds missing columns for legacy DBs (e.g., `must_change_password`, class stream/standard, `marks.class_id`, lockout fields)
- Backfills `marks.class_id` from `students.class_id` when null
- Initializes `classes.last_promoted_year` from settings where missing
- Subject name normalization/renaming
- Audit log retention cleanup (older than 3 days)

## Route inventory (Flask source routes)

### Auth/account/session
- `/`, `/login`, `/logout`, `/forgot-password`, `/change-password`, `/profile`

### Dashboard/overview/analytics
- `/dashboard`, `/headmaster/overview`, `/analytics`

### Students/classes/subjects/exams/users
- `/students`
- `/students/add`, `/students/add-bulk`, `/students/edit/<id>`, `/students/delete/<id>`, `/students/restore/<id>`, `/students/purge/<id>`
- `/classes`, `/classes/add`, `/classes/delete/<id>`
- `/promote`
- `/subjects`, `/subjects/delete/<id>`
- `/examinations`, `/examinations/delete/<id>`
- `/users`, `/users/add`, `/users/edit/<id>`, `/users/toggle/<id>`, `/users/delete/<id>`

### Marks/results workflow
- `/marks`
- `/marks/<exam_id>/<class_id>`
- `/marks/submit/<exam_id>/<class_id>`
- `/results`
- `/results/review/<exam_id>/<class_id>`
- `/results/review/<exam_id>/<class_id>/download`

### Reports/history/records
- `/records`
- `/reports`
- `/students/<student_id>/history`
- `/reports/student/<student_id>/<exam_id>`
- `/reports/student/<student_id>/<exam_id>/download`

### Settings/backups/audit
- `/settings`
- `/settings/backup/create`
- `/settings/backup/download/<filename>`
- `/settings/backup/restore/<filename>`
- `/settings/backup/upload-restore`
- `/settings/backup/delete/<filename>`
- `/audit-logs`

### APIs/media
- `/media/<path>`
- `/api/detect-gender`
- `/api/keep-alive`
- `/api/students-by-class/<class_id>`
- `/api/students-with-marks/<class_id>/<exam_id>`

## Menu + feature inventory from UI

Sidebar/menu from `base.html` includes:
- Dashboard
- Students
- Classes (headmaster)
- Promote
- Subjects
- Examinations
- Marks Entry
- Review & Approval (headmaster)
- Headmaster Overview (headmaster)
- Performance Analytics
- Reports
- Exam Records
- Users (headmaster)
- Settings (headmaster)
- Audit Log (headmaster)
- Profile / Change Password / Logout

## Forms, buttons, tables, modals/pagination behavior to preserve

- Login/forgot/change password/profile forms
- Student add/edit/bulk-add forms and table listing with filters/search
- Class CRUD and promotion actions
- Subject and exam CRUD
- User CRUD, activate/deactivate/toggle, role/class assignment
- Marks entry grid and submit action
- Result review approve/return actions with required remarks on return
- Settings form and DB backup create/download/restore/delete/upload restore actions
- Tables/cards/alerts with Bootstrap layout and custom JS behaviors

## Auth/session/security behavior inventory

- `login_required` and headmaster-only guard behavior
- Role-based authorization and class teacher restrictions (server-side)
- Account lockout after failed attempts (`MAX_LOGIN_ATTEMPTS`, lockout minutes)
- Session permanence + idle keep-alive endpoint and client auto-logout warning
- Password change flow with `must_change_password` support
- No-cache response headers, error handlers (404/500/global exception)

## Validation/business logic inventory

- Required field and uniqueness checks for students/users/exams
- Numeric mark validation (`0..100`) and incomplete-mark submission prevention
- Review return requires remarks
- Backup upload file/SQLite validity checks
- Grade mapping by configured boundaries
- Totals/average/grade/position calculations with tie-aware ranking
- Analytics grade distributions and school/class stats

## Result workflow/status transitions inventory

- Statuses: `draft → submitted → under_review → approved`
- Return path: `under_review/approved? -> returned` (remarks required)
- Class/exam status tracked in `exam_class_status` with submitter/approver timestamps
- Status history/audit actions displayed in marks/review pages

## Reports/PDF/printing inventory

- Student report view and PDF download
- Class results review view and PDF download
- Print-friendly view sections and JS print helper
- Includes school/exam/class/student/performance data, grades, totals, averages, positions, remarks

## Search/filter/audit/error/flash/redirect inventory

- Search/filter in students list, records, report selectors
- Flash messages used broadly for success/error feedback
- Redirect-driven post/redirect/get flows throughout
- Audit log entries for sensitive operations and display page

## Missing Laravel equivalents (current state)

As of this scan, all Laravel equivalents are missing because no Laravel project currently exists in the repository. Required missing equivalents include:
- Laravel routes/controllers/middleware for every Flask endpoint
- Eloquent models/migrations preserving current schema/constraints/legacy compatibility
- Auth/session/role checks equivalent to Flask decorators and security behavior
- Blade templates/components replicating all existing Jinja views/macros/UI
- Static assets wiring and JS behaviors
- PDF/report generation and print routes
- Backup management and audit logging features
- Tests/verification suite for migrated behavior parity

## Flask → Laravel feature mapping baseline

- Flask route handlers (`app.py`) → `routes/web.php` + controllers/services
- Flask decorators (`login_required`, `headmaster_required`) → Laravel middleware/policies/gates
- SQLite queries (`database.py` + `app.py`) → Eloquent models / Query Builder preserving SQL semantics
- Jinja templates (`templates/*.html`) → Blade templates/components/partials
- Flask flash (`flash`) → Laravel session flash (`session()->flash()`)
- Flask sessions → Laravel session config/middleware
- Flask PDF (`pdf_report.py`) → Laravel PDF service (e.g., Dompdf/snappy equivalent)
- Flask startup compatibility migrations (`init_db`) → safe Laravel migrations + startup compatibility command/service

