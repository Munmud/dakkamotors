# Dakka Motors

Bilingual (English / 日本語) used-car listing site for **dakkamotors.com**. Visitors browse
inventory, open a car's detail page, and call to inquire. Staff manage inventory through
Django admin.

| | |
|---|---|
| **Backend** | Django 5 + Django REST Framework, deployed to AWS Lambda via Zappa |
| **Frontend** | React 19 (Vite), served from S3 behind CloudFront |
| **Database** | Aurora Serverless v2 PostgreSQL (scales to zero); SQLite locally |
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
infra/              CloudFormation: network + database, and edge (CDN/cert)
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
python manage.py migrate           # creates db.sqlite3
python manage.py createsuperuser
python manage.py runserver
```

With no `DATABASE_URL` set, the backend falls back to SQLite — you do not need AWS or a
Postgres server to run it.

- API: <http://localhost:8000/api/cars/>
- Admin: <http://localhost:8000/api/admin/>

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
| `DATABASE_URL` | Postgres URL. **Unset locally** to use SQLite. |
| `ALLOWED_HOSTS` | Comma-separated hostnames. |
| `AWS_STORAGE_BUCKET_NAME` | Media/static bucket. Unset locally to store uploads on disk. |

### `frontend/.env`

| Variable | Purpose |
|---|---|
| `VITE_API_BASE_URL` | API root. `http://localhost:8000/api` locally, `/api` in production. |
| `VITE_CONTACT_PHONE` | Number behind the **Call Us** button. |

---

## Deployment

Pushing to `main` deploys automatically:

- changes under `backend/**` → tests run, then `zappa update production` and `migrate`
- changes under `frontend/**` → `npm run build`, sync to S3, CloudFront invalidation

GitHub Actions authenticates to AWS through **OIDC** — there are no long-lived AWS keys in
the repository or its secrets.

First-time infrastructure setup is documented in [`docs/INFRA.md`](docs/INFRA.md).
