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
python manage.py runserver

# Tests. DynamoDB Local is required.
docker compose up -d dynamodb          # from the repo root
cd backend
DYNAMODB_ENDPOINT_URL=http://localhost:8123 DDB_TABLE=dakkamotors_test \
  AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local \
  python manage.py test

# Frontend
cd frontend && npm ci && npm run dev
npm run check-i18n                     # en/ja parity, plus a warning for unused keys
```

Deploys are automatic: push to `main`, path-filtered GitHub Actions workflows deploy the
backend (Zappa) and the frontend (S3 + CloudFront invalidation) via OIDC. No static keys.

Migration commands (see `docs/INFRA.md`):

```bash
git checkout pre-dynamo    # the only commit that can still run the exporter
python manage.py export_aurora --out ./migration/<date>/
python manage.py import_dynamo --from ./migration/<date>/  # on the new code
python manage.py reconcile_counters [--fix]                # after a restore, or on doubt
```

Run the import from a laptop with SSO credentials rather than through `zappa manage` --
it is an ops script, and nothing about it needs a Lambda.

## The data layer: DynamoDB and Cognito

**There is no relational database and no Django auth.** The ORM models,
`django.contrib.auth`, `django.contrib.sessions`, `django.contrib.admin` and
`cars/migrations/` are gone; `DATABASES` is `{}`, so anything reaching for the ORM fails
at the call rather than quietly opening SQLite. `cars/store/` and `cars/cognito.py` are
the only ways in.

**The cutover has run** (2026-09-18) and **Aurora is deleted**, leaving one final
snapshot. `main` is deployed, the Lambda is out of the VPC, and the site serves from
DynamoDB and Cognito. RDS was $13.26 of a $15.75 monthly bill; the bill is now about
$1.60, nearly all the Route 53 hosted zone.

`backend/cars/store/` is the data layer: PynamoDB 6.1, one table, single-table design with
a discriminator. Cognito holds identity; DynamoDB holds only what Cognito has nowhere to
put (a pending sign-up's name and phone, reset tokens, carried-over password hashes).
**The pool sends no email at all** -- verification and reset messages go through Brevo,
bilingual and branded. Do not wire it to SES; that is where the previous attempt stalled.

Passwords cannot be migrated -- Cognito will not accept a hash on `AdminCreateUser` -- so
a `UserMigration` Lambda trigger (inline in `infra/data.yaml`) verifies the carried-over
Django hash on a customer's first sign-in. `tests_user_migration.py` extracts that inline
source from the template and tests it against hashes Django actually produces, so the
thing that will run is the thing that was checked.

Ids are strings, so URL patterns take `<str:pk>`. A slot's id is derived from (schedule,
start time) -- that is what makes materialising slots idempotent, and why two fixtures
wanting distinct slots at the same instant need distinct rules.

### Rules

* **Never build a `pk`/`sk` inline.** Every key comes from `store/keys.py`.
* **Never index `cancellation_reasons` by hand.** PynamoDB regroups transaction items by
  operation type (ConditionCheck, Delete, Put, Update) regardless of call order, so the
  list is parallel to *that*, not to the order you added them. Use the labels
  `store/txn.py` provides. Getting this wrong misattributes a failure and tells the
  customer the wrong thing.
* **Never write an unconditional `UpdateItem` against an item that may not exist.** It is
  an upsert, and the stub it creates carries no discriminator -- invisible to every
  polymorphic read in this package, and enough to block the real item from ever being
  created. Guard with `.pk.exists()`. This cost real debugging time once already.
* **`store/questions.py` is the only permitted writer of `is_published`.** That
  exclusivity replaces the `CheckConstraint` DynamoDB cannot express, and a test enforces
  it mechanically.
* **Deleting a car must clear its `CARID#` pointer too.** It lives in its own partition,
  so neither the car's partition sweep nor the guards touch it -- `store.cars.delete`
  does it explicitly. A pointer outliving its car makes `config/urls.py` **301** an old
  numeric URL to a slug that no longer resolves, and a cached 301 to a 404 is worse than
  the 404 because nothing asks again. This shipped broken once; there is a test.
* **Test truncation drops to the raw client** (`tests_store.truncate_table`), for the same
  discriminator reason. `tests_store.ensure_table` creates the table, and **both** entry
  points call it -- Django orders tests by module, so `tests.py` runs before
  `tests_store.py` and leaning on the other one having gone first is how the suite came to
  pass locally against a leftover container and fail against a fresh one.

### Staff pages

`cars/staff/` is server-rendered and replaces `django.contrib.admin` entirely: cars,
images, bookings, slots, schedules, questions, customers and staff accounts.
`django.contrib.admin` is built on `QuerySet` and `ModelForm`, so an entity that leaves
the ORM takes its admin page with it -- which is why each page was built in the same step
as its store module.

Authentication is Cognito's hosted UI, isolated in `staff/auth.py`; the views, forms and
templates never learn how somebody signed in. **`staff/permissions.py` is the whole
policy.** `OWNER_ONLY` keeps staff administration to owners, which is stricter than the
sandbox it replaces: whoever can edit staff accounts can open the owner's, so the
escalation is refused before a page is reached rather than by four guards agreeing.

### Getting a staff account

**Nobody registers as staff.** Self sign-up exists, but it is the customer path and
produces an account in no groups. Two independent things stop that becoming staff access:
the staff pages accept only tokens minted by the *staff* app client, which has no
password auth flow at all, and group membership is admin-only.

An owner adds people at `/api/staff/accounts/add/`. **The pool sends no email**, so the
page shows the temporary password once, in the response to the request that created it --
pass it on there and then. Cognito forces a change at first sign-in.

The form offers `ASSIGNABLE_GROUPS`, which is `inventory-managers` only. `owners` is
deliberately absent, so a POST carrying it fails validation rather than being quietly
dropped -- which means **a second owner can only be made from the CLI**:

```bash
POOL=ap-northeast-1_czNqEUiq5
aws cognito-idp admin-create-user --region ap-northeast-1 --user-pool-id $POOL   --username "$EMAIL"   --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true   --temporary-password '<strong>' --message-action SUPPRESS
for g in staff owners; do
  aws cognito-idp admin-add-user-to-group --region ap-northeast-1     --user-pool-id $POOL --username "$EMAIL" --group-name $g
done
```

`--message-action SUPPRESS` is required, not tidiness: the pool has no
`EmailConfiguration`, so Cognito has nothing to send an invitation with and errors if
asked. The same command is how the **first** owner is made on a fresh pool, which is
otherwise a chicken-and-egg -- the page that creates accounts needs an owner signed in.

A temporary password lasts **seven days** and Cognito forces a change on first use, so
its real lifetime is one sign-in. If one expires unused, reissue rather than creating a
second account -- the address is the username, so a second `admin-create-user` fails with
`UsernameExistsException`:

```bash
aws cognito-idp admin-set-user-password --region ap-northeast-1 --user-pool-id $POOL   --username "$EMAIL" --password '<strong>' --no-permanent
```

### Signing a test in

`tests_fake_cognito.py`. Both cookie flows resolve `cars.authentication.verify` at call
time, so patching that one name covers DRF and the staff pages, and **nothing in
production branches on being tested**. Use `sign_in(self.client, user)`, or
`staff=True` for the staff pages.

moto is used only where the token itself is the subject -- `tests_cognito.py` and
`tests_staff_auth.py` mint and verify real RS256 signatures. It is deliberately not used
for the rest: moto is an optional dependency, so those tests would be `skipUnless`-gated
and would vanish on any machine without `requirements-dev.txt`.

Every class is a `SimpleTestCase`. That is load-bearing rather than tidiness: with no
database configured, an ORM call that survived the teardown fails instead of quietly
opening SQLite. `tests_store.count_dynamo_calls` replaces `assertNumQueries` and is worth
more than it was -- the car detail page is deliberately one Query, and a per-question read
in a loop is exactly what would undo that.

## Conventions

**Rules live in domain modules, not views.** `booking.py`, `qa.py`, `notifications.py`
hold every check, so the same rule applies from the API, a management command, curl or a
test. Views are thin and translate errors into responses. The store raises typed errors
and knows no wording; the domain module owns every sentence a customer reads.

**Comments explain why, not what.** The codebase is deliberately heavy on rationale --
why a slug never changes, why `bump_car` exists, why the pool sends no email. Match
that. A comment that restates the code is noise; one that records a decision is the most
valuable thing in the file.

**Tests assert behaviour and wording.** Customer-facing strings are asserted verbatim in
`cars/tests.py`. If a change makes you edit one of those strings, suspect the change.

**Nothing runs on a timer.** Slots are materialised when availability is read, not by a
cron job. This began as a way to let Aurora scale to zero, and Aurora is gone; it
survives because it is one fewer moving part, and because the expiry sweeps it replaced
are now TTL attributes the table handles itself.

**Uploads go straight to S3.** Lambda has a ~4.5 MB request ceiling, so the staff pages
sign a presigned POST and the browser uploads directly. `direct-upload.js` is
progressive enhancement -- file inputs stay file inputs, so a JS failure falls back to a
normal upload.

**Secrets come from SSM at settings import**, via `config/ssm.py`, which runs only when
`AWS_LAMBDA_FUNCTION_NAME` is set -- so tests and local development make no network call,
and a real environment variable always wins. Zappa's `remote_env` is gone: it existed
because the VPC put the SSM API out of reach, and it meant every secret lived in two
places that had to be kept in step by hand. Non-secrets live in `zappa_settings.json`,
in git, where they can be reviewed.

**Email is queued, never sent inline.** `mail.queue_email` writes JSON to an S3 outbox
and a second Lambda sends it via Brevo (not SES). Brevo is reachable directly now, so
the outbox is a choice rather than a constraint: it is fire-and-forget by design, and a
customer's booking must never fail -- or wait 600ms -- because of an email.

**The frontend is bilingual.** Every user-visible string goes through `react-i18next`
with keys in both `en.json` and `ja.json`. `npm run check-i18n` fails the build otherwise.
It also warns about keys no component references -- comparing the two files only against
each other cannot catch a key both of them have and nothing reads, which is how seven
accumulated. That half warns rather than fails, because `t(`status.${x}`)` cannot be
resolved statically and a check that cries wolf gets disabled.

## Things that will bite

* **There are no migrations.** A schema change is a code change to `cars/store/`, and
  an attribute that is not written is simply absent from an item rather than NULL. Adding
  one is free; changing the meaning of an existing one needs a backfill you write.
* **`/api/cars/*` is a separate CloudFront behaviour** that allows only GET/HEAD/OPTIONS
  and strips cookies. A POST there is refused by the CDN with no Django log line. New
  authenticated endpoints must not live under that prefix.
* **The Lambda is no longer in a VPC.** That retired the hand-copy of secrets into
  `config/env.json` and demoted the S3 outbox from necessity to choice. It has **not**
  yet retired the inline image-resize budget in `tasks.py` -- `lambda:InvokeFunction` is
  reachable now, so that can become a real async invoke, and it is the last outstanding
  item from the cutover. `vpc_config` in `zappa_settings.json` holds empty lists and
  **the key must stay**: Zappa only sends `VpcConfig` when it is present, so deleting the
  key leaves a deployed function attached to subnets nothing mentions.
* **DynamoDB reserved keywords** include `capacity`, `status`, `order`, `year` and
  `name`, all of which appear in this schema. PynamoDB aliases them automatically; raw
  boto3 does not.
* **Cognito's `Schema` is effectively immutable.** CloudFormation fails rather than
  replaces on most edits and there is no API to remove a custom attribute, so a wrong
  attribute list means rebuilding the pool and losing every user.
* **An access token's `username` claim is the sub, not the email**, because the pool uses
  email as the username attribute. Deriving an address from it would look right and be a
  UUID.
* **`dakkamotors-core` holds both S3 buckets.** It also held the VPC and Aurora until
  September 2026, and removing those was a stack **update**, never `delete-stack` -- that
  would have taken the site and every photo with it. The same applies to anything removed
  from it in future. `infra/network-db.yaml` keeps its filename for a related reason:
  CloudFormation identifies a stack by name, and a renamed file invites somebody to
  create a second one.
