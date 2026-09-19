# Dakka Motors

Bilingual (English / 日本語) used-car listing site for **dakkamotors.com**. Visitors browse
inventory, open a car's detail page, ask a question, and book a test drive. Staff
answer, confirm and manage stock through server-rendered pages at `/api/staff/`, signed in
through Cognito's hosted UI.

| | |
|---|---|
| **Backend** | Django 5 + Django REST Framework, deployed to AWS Lambda via Zappa |
| **Frontend** | React 19 (Vite), served from S3 behind CloudFront |
| **Data** | DynamoDB, single table, on-demand; DynamoDB Local for development |
| **Identity** | Amazon Cognito; the pool sends no email, Brevo does |
| **Media** | S3, served through CloudFront |
| **Region** | `ap-northeast-1` (Tokyo) — ACM cert + CloudFront in `us-east-1` (AWS requirement) |
| **Currency** | JPY |

Frontend and API are served from the **same origin** (`/` and `/api/*` are two behaviors on
one CloudFront distribution), so there is no CORS configuration anywhere in this project.

---

## Repository layout

```
backend/            Django project (config) + cars app
frontend/           React app (Vite)
infra/              CloudFormation: storage, data (DynamoDB + Cognito), edge (CDN/cert)
.github/workflows/  CI/CD
docs/               TODO.md launch checklist, INFRA.md runbook
```

---

## Local development

Requires **Python 3.13**, **Node 20+**, and **git**.

### Backend

```bash
cd backend
python -m venv .venv
source .venv/Scripts/activate      # Windows (Git Bash);  .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

cp .env.example .env               # defaults are fine for local work
python manage.py runserver
python manage.py runserver
```

There is no relational database. The store runs against DynamoDB Local (`docker compose
up -d dynamodb`) and identity against Cognito, so you do not need AWS or a
Postgres server to run it.

- API: <http://localhost:8000/api/cars/>
- Staff pages: <http://localhost:8000/api/staff/cars/>

The staff pages need Cognito. Without `COGNITO_DOMAIN` set they return 503 rather than
pretending to work -- there is no second way in now that the Django admin has gone.

### Frontend

```bash
cd frontend
npm install
cp .env.example .env               # points at http://localhost:8000/api
npm run dev
```

Open <http://localhost:5173>. Run both servers at once for a working local site.

### Tests

```bash
cd backend && python manage.py test
```

---

## Configuration

### `backend/.env`

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Django secret. A throwaway default ships for local use. |
| `DEBUG` | `True` locally, `False` in production. |
| `ALLOWED_HOSTS` | Comma-separated hostnames. |
| `AWS_STORAGE_BUCKET_NAME` | Media/static bucket. Unset locally to store uploads on disk. |
| `DYNAMODB_ENDPOINT_URL` | Points the store at DynamoDB Local. **Never set in production.** |
| `DDB_TABLE` | Table name. Defaults to `dakkamotors`; use `dakkamotors_test` for tests. |
| `COGNITO_POOL_ID` | User pool. Without it the auth endpoints cannot work. |
| `COGNITO_CUSTOMER_CLIENT_ID` | App client for customers. No secret. |
| `COGNITO_STAFF_CLIENT_ID` / `COGNITO_STAFF_CLIENT_SECRET` | Hosted-UI client for staff. |
| `COGNITO_DOMAIN` | Hosted-UI domain. Unset means the staff pages return 503. |
| `COGNITO_ENDPOINT_URL` | Points Cognito at a local stand-in. Tests only. |
| `OUTBOX_BUCKET`, `MAIL_FROM`, `MAIL_REPLY_TO`, `STAFF_ALERT_EMAIL` | The S3 outbox mailer. |

In production only `SECRET_KEY` and `COGNITO_STAFF_CLIENT_SECRET` come from SSM, read at
settings import by `backend/config/ssm.py`. Everything else is a plain entry in
`backend/zappa_settings.json`, in git, where it can be reviewed.

### `frontend/.env`

| Variable | Purpose |
|---|---|
| `VITE_API_BASE_URL` | API root. **`/api` in both** — see below. |
| `VITE_CONTACT_PHONE` | Number behind the **Call Us** button. |

`VITE_API_BASE_URL` is `/api` locally as well as in production, and that is deliberate:
Vite proxies `/api` to Django (`vite.config.js`), so the browser only ever talks to one
origin. That reproduces the production arrangement, where CloudFront serves the app and
the API together — which is why **this project has no CORS configuration at all**. Point
it at `http://localhost:8000/api` and requests become cross-origin and fail.

---

## Deployment

Pushing to `main` deploys automatically:

- changes under `backend/**` → tests run, then `zappa update production`, `check --deploy`, `collectstatic`, and a smoke test that gates the deploy
- changes under `frontend/**` → `npm run build`, sync to S3, CloudFront invalidation

GitHub Actions authenticates to AWS through **OIDC** — there are no long-lived AWS keys in
the repository or its secrets.

First-time infrastructure setup is documented in [`docs/INFRA.md`](docs/INFRA.md).
