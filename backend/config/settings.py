"""
Django settings for the Dakka Motors backend.

Configuration comes from the environment (django-environ), read from a local `.env`
file during development and from Lambda environment variables (populated from SSM
Parameter Store) in production.

Two deliberate fallbacks keep local development free of cloud dependencies:
  * no DATABASE_URL            -> SQLite
  * no AWS_STORAGE_BUCKET_NAME -> uploads on local disk
"""

import sys
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    SECRET_KEY=(str, "dev-only-insecure-key-do-not-use-in-production"),
    DATABASE_URL=(str, ""),
    AWS_STORAGE_BUCKET_NAME=(str, ""),
    AWS_S3_REGION_NAME=(str, "ap-northeast-1"),
    MEDIA_CUSTOM_DOMAIN=(str, ""),
    CSRF_TRUSTED_ORIGINS=(
        list,
        ["https://dakkamotors.com", "https://www.dakkamotors.com"],
    ),
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

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "storages",
    "cars",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------

if env("DATABASE_URL"):
    DATABASES = {"default": env.db("DATABASE_URL")}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# Connections must close at the end of each request. Aurora Serverless v2 cannot scale
# down to zero while a connection is held open, and persistent connections are useless
# in Lambda anyway.
DATABASES["default"]["CONN_MAX_AGE"] = 0

# Password hashing is deliberately slow, which is right in production and painful in a
# suite that creates dozens of accounts - it took the staff-permission tests from a few
# seconds to well over a minute. Only ever applied while running tests.
if "test" in sys.argv:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
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
    # Session only. DRF's default also enables BasicAuthentication, which accepts a
    # password on every request and has no business on a public API - the app is
    # same-origin and uses the session cookie.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {
        # Applied to registration and login only. Without email verification an
        # account costs nothing to create, so the sign-up endpoint needs a ceiling.
        "auth": "20/hour",
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
    # silently breaks the admin: the login form posts to /production/api/admin/login/
    # and lands in the frontend. Clearing the script name keeps generated URLs rooted.
    FORCE_SCRIPT_NAME = ""
    pass
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
