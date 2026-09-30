# Dakka Motors — v1 Launch TODO

Goal: a minimal, live site at **dakkamotors.com** where anyone can browse used cars from a home page, open a car's detail page, and call to inquire. Staff manage inventory through server-rendered pages behind Cognito. Stack: **Django (API) + React (UI)**, hosted on AWS as cheaply and serverlessly as possible, with CI/CD. Bilingual: **English + Japanese**. Currency: **JPY**.

Future features are out of scope for v1 — not listed here on purpose.

---

## Phase 0 — Prerequisites
- [x] AWS account created, billing alarm set (e.g. $10/$25 thresholds)
- [x] Access to the `dakkamotors.com` domain registrar (or already using Route53)
- [x] Local tools installed: `git`, `python 3.12+`, `node 20+`, `awscli` (configured with an admin profile for one-time setup), `pip install zappa`
- [x] GitHub repo ready: https://github.com/Munmud/dakkamotors

## Phase 1 — Repo scaffolding
- [x] `git init`, add remote: `git remote add origin https://github.com/Munmud/dakkamotors`
- [x] Create structure:
  ```
  dakkamotors/
    backend/      # Django + DRF
    frontend/     # React (Vite)
    docs/
    .github/workflows/
  ```
- [x] Root `.gitignore` (Python, Node, `.env`, `*.sqlite3`, `zappa_settings.json` secrets, `staticfiles/`)
- [x] `README.md` with local dev setup steps
- [x] Initial commit, push to `main`

## Phase 2 — Backend (Django + DRF)
- [x] `django-admin startproject config backend` + `python manage.py startapp cars`
- [x] Install: `django`, `djangorestframework`, `django-environ`, `django-storages`, `boto3`, `django-cors-headers` (only needed if not using same-origin CloudFront routing), `psycopg2-binary`
- [x] `Car` model in `cars/models.py`:
  - `brand`, `grade`, `model_name`, `model_code`, `chassis_number` (unique), `manufacture_year`, `fuel_type` (choices), `seat_capacity`, `color`
  - `price_jpy` (nullable — blank means "call for price")
  - `status` (choices: `available` / `reserved` / `sold`, default `available`)
  - `description_en`, `description_ja` (both optional)
  - `created_at`, `updated_at`
- [x] `CarImage` model: FK to `Car`, `image` (S3-backed `ImageField`), `is_primary`, `order`
- [x] Register both in `cars/admin.py` (`CarImage` as inline on `CarAdmin`); list_display, list_filter (`status`, `fuel_type`, `brand`), search_fields
- [x] DRF serializers: `CarListSerializer` (card fields + primary image), `CarDetailSerializer` (all fields + image gallery)
- [x] DRF viewsets/URLs: `GET /api/cars/` (list, default filter `status=available`, paginated), `GET /api/cars/<id>/`
- [x] Settings via `django-environ` reading from `.env` locally / SSM Parameter Store in prod: `SECRET_KEY`, `DEBUG`, `DATABASE_URL`, `AWS_STORAGE_BUCKET_NAME`
- [x] `django-storages` configured for S3 (static + media in the same bucket, separate prefixes)
- [x] Local dev: SQLite fallback so you don't need RDS just to run `runserver`
- [x] Create Django superuser locally, confirm admin works, add the sample Daihatsu Tanto as a test record

## Phase 3 — Frontend (React)
- [x] `npm create vite@latest frontend -- --template react`
- [x] Install: `react-router-dom`, `react-i18next`, `i18next`, `axios`
- [x] `src/i18n/en.json`, `src/i18n/ja.json` — UI strings only (nav, buttons, labels, "Call us")
- [x] Language toggle (EN/JA) in header, persisted (e.g. localStorage)
- [x] Routes: `/` → Home, `/cars/:id` → Car Detail
- [x] **Home page**: fetch `/api/cars/`, grid of car cards (primary photo, brand + grade, year, price in ¥ or "Call for price"), links to detail page
- [x] **Car Detail page**: fetch `/api/cars/:id/`, photo gallery, full specs table, description in current language (fallback to whichever is filled), prominent **Call Us** button using `tel:` link with phone number from an env var (`VITE_CONTACT_PHONE`) — placeholder until you provide the real number
- [x] API base URL from `VITE_API_BASE_URL` env var
- [x] Basic responsive layout (mobile-first — most buyers will browse on phones)
- [x] `npm run build` produces `frontend/dist`

## Phase 4 — AWS infrastructure (cheapest, serverless-first)
- [x] S3 bucket `dakkamotors-frontend` (static site assets, private + CloudFront OAC)
- [x] S3 bucket `dakkamotors-backend-media` (Django static + car images, private + CloudFront OAC)
- [x] RDS PostgreSQL `db.t4g.micro` (free tier), in a VPC, security group open only to the Lambda's security group
- [x] Add a **free S3 Gateway VPC Endpoint** to that VPC (avoids needing a ~$32/mo NAT Gateway for Lambda-in-VPC to reach S3)
- [x] SSM Parameter Store (SecureString): `DJANGO_SECRET_KEY`, `DB_PASSWORD`, etc. (not Secrets Manager — avoids per-secret cost)
- [x] `backend/zappa_settings.json`: Lambda config incl. `vpc_config` (subnets + security group), env vars pulled from SSM
- [x] First manual deploy: `zappa deploy production`, then `zappa manage production "migrate"`, then `zappa manage production "createsuperuser"`
- [x] One CloudFront distribution for dakkamotors.com with two behaviors:
  - default (`/*`) → S3 frontend bucket
  - `/api/*` → API Gateway (Zappa's endpoint) — keeps frontend + API same-origin, no CORS needed
- [x] Route53 hosted zone for `dakkamotors.com`, NS records set at the registrar
- [x] ACM certificate in `us-east-1` for `dakkamotors.com` (+ `www`), validated via DNS, attached to CloudFront

## Phase 5 — CI/CD (GitHub Actions)
- [x] Create GitHub OIDC identity provider in AWS IAM + a deploy role with least-privilege policy (S3, Lambda, API Gateway, CloudFront invalidation, SSM read) — no long-lived AWS keys in GitHub
- [x] `.github/workflows/backend.yml` — triggers on push to `main` touching `backend/**`:
  1. install deps, run `python manage.py test`
  2. `zappa update production`
  3. `zappa manage production "migrate"`
- [x] `.github/workflows/frontend.yml` — triggers on push to `main` touching `frontend/**`:
  1. `npm ci && npm run build`
  2. `aws s3 sync dist/ s3://dakkamotors-frontend --delete`
  3. `aws cloudfront create-invalidation`

## Phase 6 — Go live
- [x] DNS cutover confirmed (dig/nslookup `dakkamotors.com` resolves to CloudFront)
- [x] ACM cert shows "Issued", HTTPS works, HTTP redirects to HTTPS
- [x] Smoke test: home page loads and lists cars, detail page loads, images load, Call Us button dials the correct number, language toggle works, Django admin login works at `/api/admin/` (or chosen path)
- [x] Add the sample Daihatsu Tanto (and any other real inventory) as the first live listings
- [ ] Announce / start marketing

---

## Test drive booking

**Removed, 2026-09-29.** Booking, slots, schedules, availability, customer accounts,
sign-up, email confirmation, password reset, per-car questions and notifications are
all deleted. Everything in this section was completed and then thrown away; it is kept
because the reasoning is the point.

A ¥1,000 Meta promotion produced ten bookings, every one cancelled and every one either
the owner's own address or a throwaway. Zero genuine leads, ever. The premise was that
somebody who tapped a video advertisement thirty seconds ago would create an account,
confirm it by email, and then commit to standing in a car park at 15:00 on Saturday.

The site now asks for a name and a phone number and the shop telephones. See
"What was removed, and why it is not coming back" in `CLAUDE.md`.

Two things were never established, and they are the reason not to be too pleased with
this: CloudFront access logging is **off**, so there is no record of whether anybody
ever arrived, and on 2026-09-23 the new ad account had **no payment method**. The
promotion may simply never have delivered.

- [ ] **Turn on CloudFront access logging**, so the next campaign has ground truth that
      does not depend on Meta. This is the open item from the above and it should be
      done before any further spend.

### Two trust signals that were considered and declined

Raised because a first-time visitor has almost nothing to go on before handing over a
phone number, and answered by the owner on 2026-09-30:

- **Opening hours: there are none to publish.** Small lot, phone answered when it is
  answered. So nothing on the site may state or imply a time, including "we will call
  you back today" — `lead.doneBody` says "as soon as we can" and should keep saying
  something of that shape, and `seo.BUSINESS` carries no `openingHours`. Do not add
  them back without the owner saying so; they were invented once already.
- **No photograph of the shop, the lot or the owner.** Not happening, so the trust the
  site can offer rests on the address (now a map link), the phone number, and the cars
  themselves. Which makes the card photo matter more, not less — the staff gallery page
  now says to choose an outside shot of the whole car, because that picture is the home
  page, the top of the car page and the first thing an advertisement lands on.

Still unanswered, and both cheap:

- [ ] **古物商許可番号** — the secondhand dealer permit number. Every legitimate
      Japanese shop displays it and its absence is what a cautious buyer notices.
- [ ] **Is the price 車両本体価格 or 支払総額?** A bare number with nothing said about
      諸費用 is the most common used-car complaint in Japan. One line under the plate
      settles it either way.

Email runs through **Brevo**, not SES. `dakkamotors.com` is authenticated (DKIM, SPF,
DMARC in Route53) and the free tier covers 300 messages a day. One message is sent now:
the staff alert when somebody leaves their number.

---

## Later

Future features (filters, multiple locations, financing calculator) will be scoped in a
future version. The contact form is no longer on this list because it is now the site.

Still open from the DynamoDB cutover:

- [ ] **Make derivative generation a real async invoke.** `tasks.py` still resizes
      photographs inline against a time budget, which is left over from the Lambda
      being in a VPC and unable to reach `lambda:InvokeFunction`. It can now.
- [ ] **Turn on CloudFront access logging** (see above).

---

## Status — 2026-09-10

**Live: https://dakkamotors.com**

DNS cutover is complete. The nameservers were updated at GoDaddy, the delegation
propagated, and the ACM certificate validated and was attached to CloudFront along with
the `dakkamotors.com` and `www.dakkamotors.com` aliases.

Verified on the live domain:

| Check | Result |
|---|---|
| `dakkamotors.com` / `www.` over HTTPS | 200 |
| HTTP | 301 to HTTPS |
| `/api/cars/` and `/api/cars/1/` | 200, returns the seeded car |
| `/cars/1` refreshed directly (SPA route) | 200 |
| `/api/cars/9999/` | 404 — API errors are not swallowed by the SPA router |
| `/static/admin/css/base.css` | 200 from private S3 via OAC |
| `/media/*` | 200 (verified with a temporary probe object, since removed) |
| Admin login at `/api/admin/` | 302 to the admin index; car list renders |
| EN / 日本語 toggle | Both render, choice persists |
| Browser console | No errors |
| Aurora | Observed at 0.0 ACU while idle — scale-to-zero working |

The only unticked item left is **announce / start marketing**, which is yours to make.

### Managing inventory

Staff pages: **https://dakkamotors.com/api/staff/cars/**. Sign in through Cognito's
hosted UI -- there is no password in SSM and no `admin` user; the Django admin and its
credentials went with the DynamoDB cutover. Accounts are managed at
`/api/staff/accounts/`, owners only.

Add a car under **Cars > Add**. Photos attach on the same page and upload straight to S3.
Tick `is_primary` on the one that should appear on the listing card. Leave `price_jpy`
blank to show "Call for price". Only `available` cars appear on the home page, but a
direct link to a reserved or sold car keeps working.

### Leftover from the previous build

CloudFormation stack `dakkamotors-dev` (the earlier SAM + Cognito version, deleted
2026-09-09) was stuck in `DELETE_FAILED` with one undeleted `CognitoEmailRole`. It is why
the domain had stopped resolving: its Route53 hosted zone went away with it, leaving the
old delegation pointing at nameservers that no longer answered.

**It is gone**, along with everything else in the account that was not this project --
removed 2026-09-19. The lesson it taught is in `docs/INFRA.md`: never put anything
DNS-related in a stack with anything else.

### One thing still to change

A demo Daihatsu Tanto (chassis `DEMO-0001`, no photos, "Call for price") is seeded by
`manage.py seed_demo_car` so the site is not empty. Delete it from
`/api/staff/cars/` once real stock is loaded.

The contact number is real: `080-9282-3601`, dialled as `+818092823601` so it works
from outside Japan too.
