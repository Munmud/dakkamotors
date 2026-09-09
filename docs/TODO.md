# Dakka Motors — v1 Launch TODO

Goal: a minimal, live site at **dakkamotors.com** where anyone can browse used cars from a home page, open a car's detail page, and call to inquire. Admin manages inventory via Django admin. Stack: **Django (API) + React (UI)**, hosted on AWS as cheaply and serverlessly as possible, with CI/CD. Bilingual: **English + Japanese**. Currency: **JPY**.

Future features are out of scope for v1 — not listed here on purpose.

---

## Phase 0 — Prerequisites
- [ ] AWS account created, billing alarm set (e.g. $10/$25 thresholds)
- [ ] Access to the `dakkamotors.com` domain registrar (or already using Route53)
- [ ] Local tools installed: `git`, `python 3.12+`, `node 20+`, `awscli` (configured with an admin profile for one-time setup), `pip install zappa`
- [ ] GitHub repo ready: https://github.com/Munmud/dakkamotors

## Phase 1 — Repo scaffolding
- [ ] `git init`, add remote: `git remote add origin https://github.com/Munmud/dakkamotors`
- [ ] Create structure:
  ```
  dakkamotors/
    backend/      # Django + DRF
    frontend/     # React (Vite)
    docs/
    .github/workflows/
  ```
- [ ] Root `.gitignore` (Python, Node, `.env`, `*.sqlite3`, `zappa_settings.json` secrets, `staticfiles/`)
- [ ] `README.md` with local dev setup steps
- [ ] Initial commit, push to `main`

## Phase 2 — Backend (Django + DRF)
- [ ] `django-admin startproject config backend` + `python manage.py startapp cars`
- [ ] Install: `django`, `djangorestframework`, `django-environ`, `django-storages`, `boto3`, `django-cors-headers` (only needed if not using same-origin CloudFront routing), `psycopg2-binary`
- [ ] `Car` model in `cars/models.py`:
  - `brand`, `grade`, `model_name`, `model_code`, `chassis_number` (unique), `manufacture_year`, `fuel_type` (choices), `seat_capacity`, `color`
  - `price_jpy` (nullable — blank means "call for price")
  - `status` (choices: `available` / `reserved` / `sold`, default `available`)
  - `description_en`, `description_ja` (both optional)
  - `created_at`, `updated_at`
- [ ] `CarImage` model: FK to `Car`, `image` (S3-backed `ImageField`), `is_primary`, `order`
- [ ] Register both in `cars/admin.py` (`CarImage` as inline on `CarAdmin`); list_display, list_filter (`status`, `fuel_type`, `brand`), search_fields
- [ ] DRF serializers: `CarListSerializer` (card fields + primary image), `CarDetailSerializer` (all fields + image gallery)
- [ ] DRF viewsets/URLs: `GET /api/cars/` (list, default filter `status=available`, paginated), `GET /api/cars/<id>/`
- [ ] Settings via `django-environ` reading from `.env` locally / SSM Parameter Store in prod: `SECRET_KEY`, `DEBUG`, `DATABASE_URL`, `AWS_STORAGE_BUCKET_NAME`
- [ ] `django-storages` configured for S3 (static + media in the same bucket, separate prefixes)
- [ ] Local dev: SQLite fallback so you don't need RDS just to run `runserver`
- [ ] Create Django superuser locally, confirm admin works, add the sample Daihatsu Tanto as a test record

## Phase 3 — Frontend (React)
- [ ] `npm create vite@latest frontend -- --template react`
- [ ] Install: `react-router-dom`, `react-i18next`, `i18next`, `axios`
- [ ] `src/i18n/en.json`, `src/i18n/ja.json` — UI strings only (nav, buttons, labels, "Call us")
- [ ] Language toggle (EN/JA) in header, persisted (e.g. localStorage)
- [ ] Routes: `/` → Home, `/cars/:id` → Car Detail
- [ ] **Home page**: fetch `/api/cars/`, grid of car cards (primary photo, brand + grade, year, price in ¥ or "Call for price"), links to detail page
- [ ] **Car Detail page**: fetch `/api/cars/:id/`, photo gallery, full specs table, description in current language (fallback to whichever is filled), prominent **Call Us** button using `tel:` link with phone number from an env var (`VITE_CONTACT_PHONE`) — placeholder until you provide the real number
- [ ] API base URL from `VITE_API_BASE_URL` env var
- [ ] Basic responsive layout (mobile-first — most buyers will browse on phones)
- [ ] `npm run build` produces `frontend/dist`

## Phase 4 — AWS infrastructure (cheapest, serverless-first)
- [ ] S3 bucket `dakkamotors-frontend` (static site assets, private + CloudFront OAC)
- [ ] S3 bucket `dakkamotors-backend-media` (Django static + car images, private + CloudFront OAC)
- [ ] RDS PostgreSQL `db.t4g.micro` (free tier), in a VPC, security group open only to the Lambda's security group
- [ ] Add a **free S3 Gateway VPC Endpoint** to that VPC (avoids needing a ~$32/mo NAT Gateway for Lambda-in-VPC to reach S3)
- [ ] SSM Parameter Store (SecureString): `DJANGO_SECRET_KEY`, `DB_PASSWORD`, etc. (not Secrets Manager — avoids per-secret cost)
- [ ] `backend/zappa_settings.json`: Lambda config incl. `vpc_config` (subnets + security group), env vars pulled from SSM
- [ ] First manual deploy: `zappa deploy production`, then `zappa manage production "migrate"`, then `zappa manage production "createsuperuser"`
- [ ] One CloudFront distribution for dakkamotors.com with two behaviors:
  - default (`/*`) → S3 frontend bucket
  - `/api/*` → API Gateway (Zappa's endpoint) — keeps frontend + API same-origin, no CORS needed
- [ ] Route53 hosted zone for `dakkamotors.com`, NS records set at the registrar
- [ ] ACM certificate in `us-east-1` for `dakkamotors.com` (+ `www`), validated via DNS, attached to CloudFront

## Phase 5 — CI/CD (GitHub Actions)
- [ ] Create GitHub OIDC identity provider in AWS IAM + a deploy role with least-privilege policy (S3, Lambda, API Gateway, CloudFront invalidation, SSM read) — no long-lived AWS keys in GitHub
- [ ] `.github/workflows/backend.yml` — triggers on push to `main` touching `backend/**`:
  1. install deps, run `python manage.py test`
  2. `zappa update production`
  3. `zappa manage production "migrate"`
- [ ] `.github/workflows/frontend.yml` — triggers on push to `main` touching `frontend/**`:
  1. `npm ci && npm run build`
  2. `aws s3 sync dist/ s3://dakkamotors-frontend --delete`
  3. `aws cloudfront create-invalidation`

## Phase 6 — Go live
- [ ] DNS cutover confirmed (dig/nslookup `dakkamotors.com` resolves to CloudFront)
- [ ] ACM cert shows "Issued", HTTPS works, HTTP redirects to HTTPS
- [ ] Smoke test: home page loads and lists cars, detail page loads, images load, Call Us button dials the correct number, language toggle works, Django admin login works at `/api/admin/` (or chosen path)
- [ ] Add the sample Daihatsu Tanto (and any other real inventory) as the first live listings
- [ ] Announce / start marketing

---

## Later
Future features (filters, WhatsApp/contact form, multiple locations, financing calculator, etc.) will be scoped in a future version — not part of this checklist.
