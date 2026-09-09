# Dakka Motors — v1 Launch TODO

Goal: a minimal, live site at **dakkamotors.com** where anyone can browse used cars from a home page, open a car's detail page, and call to inquire. Admin manages inventory via Django admin. Stack: **Django (API) + React (UI)**, hosted on AWS as cheaply and serverlessly as possible, with CI/CD. Bilingual: **English + Japanese**. Currency: **JPY**.

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
- [ ] DNS cutover confirmed (dig/nslookup `dakkamotors.com` resolves to CloudFront)
- [ ] ACM cert shows "Issued", HTTPS works, HTTP redirects to HTTPS
- [ ] Smoke test: home page loads and lists cars, detail page loads, images load, Call Us button dials the correct number, language toggle works, Django admin login works at `/api/admin/` (or chosen path)
- [x] Add the sample Daihatsu Tanto (and any other real inventory) as the first live listings
- [ ] Announce / start marketing

---

## Later
Future features (filters, WhatsApp/contact form, multiple locations, financing calculator, etc.) will be scoped in a future version — not part of this checklist.

---

## Status — 2026-09-09

Everything above is done and deployed except the four items still unticked, all of
which wait on one thing: **the nameservers at GoDaddy**.

**Live now:** https://d2y8zvbmyas7y1.cloudfront.net

`dakkamotors.com` is delegated to Route53 nameservers that no longer exist — the hosted
zone they belonged to was deleted at some point, so those servers answer `REFUSED` and
the domain does not resolve at all. A new hosted zone was created for this project and
it was assigned a **different** set of four nameservers, so GoDaddy has to be updated:

```
ns-1371.awsdns-43.org
ns-148.awsdns-18.com
ns-1655.awsdns-14.co.uk
ns-691.awsdns-22.net
```

The TLS certificate validates over DNS, so it stays `PENDING_VALIDATION` until that
change lands. Nothing else is blocked, and no re-request is needed.

Once GoDaddy is updated, one command finishes the launch:

```bash
bash infra/finish-dns-cutover.sh
```

It waits for the certificate, attaches it to CloudFront with the `dakkamotors.com`
aliases, switches car-photo URLs to the real domain, and invalidates the cache.

### Managing inventory

Admin: **https://d2y8zvbmyas7y1.cloudfront.net/api/admin/** (becomes
`https://dakkamotors.com/api/admin/` after cutover). Username `admin`; the password is
in SSM, never in this repo:

```bash
MSYS_NO_PATHCONV=1 aws ssm get-parameter --name "/dakkamotors/ADMIN_PASSWORD"   --with-decryption --query Parameter.Value --output text
```

Add a car under **Inventory > Cars**. Photos are attached inline on the same page; tick
`is_primary` on the one that should appear on the listing card. Leave `price_jpy` blank
to show "Call for price". Only cars with status `available` appear on the home page, but
a direct link to a reserved or sold car keeps working.

### Leftover from the previous build

CloudFormation stack `dakkamotors-dev` (the earlier SAM + Cognito version, deleted
2026-09-09) is stuck in `DELETE_FAILED` with one undeleted `CognitoEmailRole`. It is
unrelated to this project and costs nothing, but it is almost certainly why the domain
stopped resolving: its Route53 hosted zone went away with it, leaving GoDaddy pointing
at nameservers that no longer answer. Left untouched.

### Two things to change when you are ready

- **Sample listing** — a demo Daihatsu Tanto (chassis `DEMO-0001`, no photos, "Call for
  price") is seeded so the site is not empty. Delete it from the admin once real stock
  is loaded.
- **Nothing else is a placeholder.** The contact number is real: `080-9282-3601`,
  dialled as `+818092823601` so it works from outside Japan too.
