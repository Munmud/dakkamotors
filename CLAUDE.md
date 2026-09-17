# Dakka Motors

A used-car dealership site for a shop in Hamura, Japan. Django + DRF on AWS Lambda,
React SPA, bilingual (English/Japanese). Buyers browse inventory, ask questions about a
car, and book test drives; staff answer, confirm and manage stock.

Everything runs in `ap-northeast-1` except the three things AWS forces into `us-east-1`
(CloudFront, its ACM certificate, and Budgets).

## Layout

```
backend/         Django 5.2 + DRF, deployed to Lambda by Zappa
  cars/          the single app -- inventory, bookings, Q&A, notifications
  config/        settings, root urls, wsgi
frontend/        React 19 + Vite, deployed to S3 behind CloudFront
infra/           hand-written CloudFormation (no CDK, no Terraform)
docs/INFRA.md    the real runbook: live resource ids, cost rationale, teardown
```

## Commands

```bash
# Backend
cd backend
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate && python manage.py runserver

# Tests. DynamoDB Local is required -- see "Migration in flight" below.
docker compose up -d dynamodb          # from the repo root
cd backend
DYNAMODB_ENDPOINT_URL=http://localhost:8123 DDB_TABLE=dakkamotors_test \
  AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local \
  python manage.py test

# Frontend
cd frontend && npm ci && npm run dev
npm run check-i18n                     # every key present in both en and ja
```

Deploys are automatic: push to `main`, path-filtered GitHub Actions workflows deploy the
backend (Zappa) and the frontend (S3 + CloudFront invalidation) via OIDC. No static keys.

## Migration in flight: Aurora -> DynamoDB

**The repo is mid-migration off Aurora PostgreSQL onto DynamoDB.** Both stores are live
at once, so read this before touching the data layer.

`backend/cars/store/` is the DynamoDB layer: PynamoDB 6.1, one table, single-table
design with a discriminator. Already moved: **notifications**, **questions**,
**bookings**, **slots**, **customers**. Still on the ORM: cars, images, schedules, auth.

Ids are strings now wherever an entity has moved, so URL patterns take `<str:pk>`, not
`<int:pk>`. A slot's id is derived from (schedule, start time) -- that is what makes
materialising slots idempotent, and why two fixtures wanting distinct slots at the same
instant need distinct rules.

Rules while both exist:

* **Never build a `pk`/`sk` inline.** Every key comes from `store/keys.py`.
* **Never index `cancellation_reasons` by hand.** PynamoDB regroups transaction items by
  operation type (ConditionCheck, Delete, Put, Update) regardless of call order, so the
  list is parallel to *that*, not to the order you added them. Use the labels that
  `store/txn.py` provides. Getting this wrong misattributes a failure and tells the
  customer the wrong thing.
* **`cars/identity.py` bridges the two worlds** (`sub_of`, `car_id_of`, `user_for_sub`).
  It exists to be deleted when Cognito lands.
* **Moving an entity means building its staff page in the same step.**
  `django.contrib.admin` is built on `QuerySet` and `ModelForm`, so an entity that
  leaves the ORM takes its admin page with it. Replacements live in `cars/staff/`,
  server-rendered, currently behind Django's `staff_member_required` -- `staff/auth.py`
  is the only module Cognito will touch.
* **`store/questions.py` is the only permitted writer of `is_published`.** That
  exclusivity is what replaces the `CheckConstraint` DynamoDB cannot express, and a test
  enforces it mechanically.

Design decisions and the full plan live in `~/.claude/plans/` and in each store module's
docstring. `docs/INFRA.md` still describes the Aurora architecture and is updated as
pieces land.

## Conventions

**Rules live in domain modules, not views.** `booking.py`, `qa.py`, `notifications.py`
hold every check, so the same rule applies from the API, a management command, curl or a
test. Views are thin and translate errors into responses. The store raises typed errors
and knows no wording; the domain module owns every sentence a customer reads.

**Comments explain why, not what.** The codebase is deliberately heavy on rationale --
why `CONN_MAX_AGE = 0`, why there is no NAT gateway, why a slug never changes. Match
that. A comment that restates the code is noise; one that records a decision is the most
valuable thing in the file.

**Tests assert behaviour and wording.** Customer-facing strings are asserted verbatim in
`cars/tests.py`. If a change makes you edit one of those strings, suspect the change.

**Nothing runs on a timer.** Slots are materialised when availability is read, not by a
cron job. This began as a way to let Aurora scale to zero; it survives because it is one
fewer moving part. Do not add a scheduled sweeper without a reason that outlives that
one.

**Uploads go straight to S3.** Lambda has a ~4.5 MB request ceiling, so the staff pages
sign a presigned POST and the browser uploads directly. `direct-upload.js` is
progressive enhancement -- file inputs stay file inputs, so a JS failure falls back to a
normal upload.

**Email is queued, never sent inline.** `mail.queue_email` writes JSON to an S3 outbox
and a Lambda outside the VPC sends it via Brevo (not SES). It is fire-and-forget by
design: a customer's booking must never fail because an email could not be written.

**The frontend is bilingual.** Every user-visible string goes through `react-i18next`
with keys in both `en.json` and `ja.json`. `npm run check-i18n` fails the build otherwise.

## Things that will bite

* **Zappa probes `/` before migrations run**, so a release adding a column fails that
  probe even when the deploy is fine. The workflow treats it as a warning and gates on a
  smoke test that runs after migrations.
* **`/api/cars/*` is a separate CloudFront behaviour** that allows only GET/HEAD/OPTIONS
  and strips cookies. A POST there is refused by the CDN with no Django log line. New
  authenticated endpoints must not live under that prefix.
* **The Lambda is in a VPC with no NAT.** It can reach Aurora and S3 and nothing else --
  no SQS, no SSM, no Lambda self-invoke. A self-invoke does not fail fast, it hangs
  until timeout and surfaces as a 504. This constraint disappears when Aurora does.
* **DynamoDB reserved keywords** include `capacity`, `status`, `order`, `year` and
  `name`, all of which appear in this schema. PynamoDB aliases them automatically; raw
  boto3 does not.
* **`SlugField` sets `db_index=True`**, so adding one and then making it unique in the
  same migration makes Postgres build the same index twice and the migration dies.
