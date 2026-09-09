"""
Django settings for the Dakka Motors backend.

Configuration comes from the environment (django-environ), read from a local `.env`
file during development and from Lambda environment variables (populated from SSM
Parameter Store) in production.

Two deliberate fallbacks keep local development free of cloud dependencies:
  * no DATABASE_URL            -> SQLite
  * no AWS_STORAGE_BUCKET_NAME -> uploads on local disk
"""

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
        "file_overwrite": False,
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
}


# --------------------------------------------------------------------------------------
# Running behind CloudFront + API Gateway
# --------------------------------------------------------------------------------------

if not DEBUG:
    # TLS terminates at CloudFront; the origin request arrives over HTTP with this header.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True
    # Django only ever sees the API Gateway host, because CloudFront forwards every
    # header except Host. The admin's CSRF check therefore relies entirely on this
    # list matching the origin the browser is actually on -- which is the CloudFront
    # domain before DNS cutover and dakkamotors.com after it. Configurable so both
    # can be trusted without a code change.
    CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
