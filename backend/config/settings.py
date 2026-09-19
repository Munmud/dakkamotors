"""
Django settings for the Dakka Motors backend.

Configuration comes from the environment (django-environ): a local `.env` during
development, and on Lambda the variables in `zappa_settings.json` plus the secrets
`config/ssm.py` pulls from Parameter Store at import.

There is no relational database at all: `cars/store/` reads and writes DynamoDB and
`cars/cognito.py` owns identity. One deliberate fallback keeps local development free of
cloud dependencies:
  * no AWS_STORAGE_BUCKET_NAME -> uploads on local disk

`DYNAMODB_ENDPOINT_URL` points the store at DynamoDB Local, and `COGNITO_ENDPOINT_URL`
does the same for a stand-in pool.
"""

import sys
from pathlib import Path

import environ

from . import ssm

BASE_DIR = Path(__file__).resolve().parent.parent

# Secrets first, before anything below reads the environment. On Lambda this is one
# SSM call per container; everywhere else it is a no-op, so tests and local development
# make no network call and a real environment variable always wins.
ssm.load()

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    SECRET_KEY=(str, "dev-only-insecure-key-do-not-use-in-production"),
    AWS_STORAGE_BUCKET_NAME=(str, ""),
    AWS_S3_REGION_NAME=(str, "ap-northeast-1"),
    MEDIA_CUSTOM_DOMAIN=(str, ""),
    CSRF_TRUSTED_ORIGINS=(
        list,
        ["https://dakkamotors.com", "https://www.dakkamotors.com"],
    ),
    # Email. Nothing is sent inline: messages are written to this bucket and a second
    # Lambda sends them via Brevo. Fire-and-forget by design -- a booking must not fail,
    # or wait on a third party, because of an email. See cars/mail.py, infra/mailer.yaml.
    OUTBOX_BUCKET=(str, ""),
    MAIL_FROM=(str, ""),
    MAIL_FROM_NAME=(str, "Dakka Motors"),
    MAIL_REPLY_TO=(str, ""),
    STAFF_ALERT_EMAIL=(str, ""),
    # DynamoDB. The table name is fixed by CloudFormation; the endpoint override is
    # empty everywhere except local development and CI, where it points at
    # DynamoDB Local. Never set it in production.
    DDB_TABLE=(str, "dakkamotors"),
    DYNAMODB_ENDPOINT_URL=(str, ""),
    # Cognito. Pool and client ids are not secrets; only the staff client secret is,
    # and that comes from SSM.
    COGNITO_POOL_ID=(str, ""),
    COGNITO_CUSTOMER_CLIENT_ID=(str, ""),
    COGNITO_STAFF_CLIENT_ID=(str, ""),
    COGNITO_DOMAIN=(str, ""),
    # A copy of the pool's JWKS, baked into the deploy. A pool publishes exactly two
    # signing keys and does not rotate them, so coupling every cold start to a Cognito
    # endpoint being reachable buys nothing. Empty means fetch over the network.
    COGNITO_JWKS_PATH=(str, ""),
    # Endpoint override for a local Cognito stand-in, the same shape as
    # DYNAMODB_ENDPOINT_URL. Empty everywhere except tests; never set in production.
    COGNITO_ENDPOINT_URL=(str, ""),
    # The only Cognito secret. From SSM in production; never in a repo.
    COGNITO_STAFF_CLIENT_SECRET=(str, ""),
)

# Read .env when present. Absent in Lambda, where real env vars are used instead.
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")


# Django only ever sees the API Gateway host in production, because CloudFront forwards
# every header except Host. Any authenticated write - the admin, and every customer
# booking - therefore rests entirely on this list matching the origin the browser is
# really on.
#
# It has to be set in development too. DRF enforces CSRF only for *authenticated*
# requests, so registering and signing in work without it and the first thing to fail is
# the first real action a signed-in customer takes.
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")
if DEBUG:
    CSRF_TRUSTED_ORIGINS = CSRF_TRUSTED_ORIGINS + [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]


# --------------------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------------------

# No `auth`, `contenttypes`, `sessions` or `admin`. Cognito holds identity and the store
# holds everything else, so there is no model layer for them to serve.
#
# `messages` stays and is database-free with CookieStorage, which is what lets every
# `messages.success(...)` string from the admin pages carry over unchanged. `staticfiles`
# needs no database either -- `collectstatic` still works.
INSTALLED_APPS = [
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "storages",
    "cars",
]

# CsrfViewMiddleware stays, and it is the piece people expect to go with sessions. With
# CSRF_USE_SESSIONS = False the token is a cookie-plus-secret construction that touches no
# database, so `/api/auth/csrf/` and all twenty exported functions in
# `frontend/src/lib/auth.js` are unchanged by any of this.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------

# Django's default message storage is FallbackStorage: cookies first, sessions when a
# message will not fit. There is no session framework any more, so that fallback raises
# rather than degrades. CookieStorage alone is database-free, and it is what lets every
# `messages.success(...)` string from the admin pages carry over word for word.
MESSAGE_STORAGE = "django.contrib.messages.storage.cookie.CookieStorage"


# There is no relational database. Django 5.2 accepts this, and every read and write
# goes through `cars/store/` to DynamoDB or through `cars/cognito.py`.
#
# Anything that reaches for the ORM from here on fails loudly at the call rather than
# quietly opening SQLite, which is the point of setting it empty rather than leaving a
# default in place.
DATABASES = {}


# --------------------------------------------------------------------------------------
# DynamoDB
# --------------------------------------------------------------------------------------

# Read by cars/store/base.py. The endpoint override is empty everywhere except local
# development and CI, where it points at DynamoDB Local. Never set it in production.
DDB_TABLE = env("DDB_TABLE")
DYNAMODB_ENDPOINT_URL = env("DYNAMODB_ENDPOINT_URL")


# --------------------------------------------------------------------------------------
# Cognito
# --------------------------------------------------------------------------------------

COGNITO_POOL_ID = env("COGNITO_POOL_ID")
COGNITO_CUSTOMER_CLIENT_ID = env("COGNITO_CUSTOMER_CLIENT_ID")
COGNITO_STAFF_CLIENT_ID = env("COGNITO_STAFF_CLIENT_ID")
COGNITO_DOMAIN = env("COGNITO_DOMAIN")
COGNITO_JWKS_PATH = env("COGNITO_JWKS_PATH")
COGNITO_ENDPOINT_URL = env("COGNITO_ENDPOINT_URL")
COGNITO_STAFF_CLIENT_SECRET = env("COGNITO_STAFF_CLIENT_SECRET")

# Cognito enforces its own password policy, and the customer-lax/staff-strict split that
# a single pool cannot express lives in `cars/auth_views.py`. Django validates no password
# here because Django is never handed one: sign-up, sign-in and reset all go to Cognito.
#
# `PASSWORD_HASHERS` survives for exactly one reader -- `tests_user_migration.py` uses
# `django.contrib.auth.hashers.make_password` to generate the pbkdf2_sha256 hashes the
# UserMigration trigger must accept. That module works without the app installed, since
# it reads only this setting.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.MD5PasswordHasher",
]


# --------------------------------------------------------------------------------------
# Internationalisation
# --------------------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Tokyo"
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------------------
# Static files and media
# --------------------------------------------------------------------------------------

AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")

# Left blank locally, which makes queue_email a no-op that just logs - development
# should never be able to email a real customer by accident.
OUTBOX_BUCKET = env("OUTBOX_BUCKET")
MAIL_FROM = env("MAIL_FROM")
MAIL_FROM_NAME = env("MAIL_FROM_NAME")
MAIL_REPLY_TO = env("MAIL_REPLY_TO")
STAFF_ALERT_EMAIL = env("STAFF_ALERT_EMAIL")
AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME")

STATIC_URL = "/static/"
MEDIA_URL = "/media/"

if AWS_STORAGE_BUCKET_NAME:
    # Both buckets sit behind CloudFront with Origin Access Control, so objects are
    # private to S3 but public through the distribution. Signed querystrings would
    # defeat CDN caching, hence querystring_auth=False.
    _s3_options = {
        "bucket_name": AWS_STORAGE_BUCKET_NAME,
        "region_name": env("AWS_S3_REGION_NAME"),
        "querystring_auth": False,
        # Also what makes the immutable cache header below safe: a re-upload becomes a
        # new object name rather than overwriting one browsers have cached for a year.
        "file_overwrite": False,
        "object_parameters": {
            "CacheControl": "public, max-age=31536000, immutable",
        },
    }
    _custom_domain = env("MEDIA_CUSTOM_DOMAIN")
    if _custom_domain:
        _s3_options["custom_domain"] = _custom_domain

    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {**_s3_options, "location": "media"},
        },
        "staticfiles": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {**_s3_options, "location": "static"},
        },
    }
else:
    STATIC_ROOT = BASE_DIR / "staticfiles"
    MEDIA_ROOT = BASE_DIR / "mediafiles"


# --------------------------------------------------------------------------------------
# REST framework
# --------------------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 12,
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    # Cookie credentials only. DRF's default also enables BasicAuthentication, which
    # accepts a password on every request and has no business on a public API - the app
    # is same-origin and sends a cookie.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "cars.authentication.CognitoCookieAuthentication",
    ],
    # DRF's default is django.contrib.auth.models.AnonymousUser, which imports the auth
    # models and through them contenttypes. Neither app is installed, so the import
    # raises and every anonymous request 500s.
    "UNAUTHENTICATED_USER": "cars.cognito.AnonymousCognitoUser",
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {
        # Applied to registration and login only. Without email verification an
        # account costs nothing to create, so the sign-up endpoint needs a ceiling.
        "auth": "20/hour",
            # Asking is authenticated, so this has to be a UserRateThrottle scope -
            # AnonRateThrottle keys on IP and returns no key at all for a signed-in
            # request, which would make it decoration.
            "ask": "10/day",
    },
}


# --------------------------------------------------------------------------------------
# Running behind CloudFront + API Gateway
# --------------------------------------------------------------------------------------

if not DEBUG:
    # TLS terminates at CloudFront; the origin request arrives over HTTP with this header.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True

    # Zappa mounts the app under the API Gateway stage, so WSGI reports SCRIPT_NAME as
    # "/production" and Django prefixes every generated URL with it. CloudFront already
    # strips that segment via the origin path, so those links point at a path that does
    # not exist publicly -- and because the SPA router rewrites extensionless paths to
    # index.html, they return the React app with a 200 instead of an obvious 404. That
    # silently broke sign-in: the form posted to /production/api/staff/... and landed in
    # the frontend. Clearing the script name keeps generated URLs rooted.
    FORCE_SCRIPT_NAME = ""

    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
