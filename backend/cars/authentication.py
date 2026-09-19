"""Turning a Cognito access token into `request.user`.

The tokens live in httpOnly cookies that Django sets, and **CSRF stays exactly as it
was**. That surprises people, so it is worth stating plainly: `CsrfViewMiddleware` needs
no database. With the default `CSRF_USE_SESSIONS = False` the token is a
cookie-plus-secret construction, so `django.contrib.sessions` could leave while CSRF
protection stays -- which is why `/api/auth/csrf/` survives unchanged and the twenty
exported functions in `frontend/src/lib/auth.js` did not have to move.

The alternative was `Authorization: Bearer` with the refresh token in `localStorage`.
That is a larger frontend change and it puts a 30-day credential somewhere JavaScript can
read it, on a site that renders customer-supplied question text. Cookies are both the
smaller diff and the safer one.
"""

import gzip
import json
import logging
import pathlib
import threading
import time

import jwt
from django.conf import settings
from rest_framework import authentication, exceptions

from . import cognito

logger = logging.getLogger(__name__)

#: Cookie names. `dm_rt` is scoped to /api/auth so the refresh token is not attached to
#: ordinary API calls.
ACCESS_COOKIE = "dm_at"
REFRESH_COOKIE = "dm_rt"
STAFF_COOKIE = "dm_st"

REFRESH_PATH = "/api/auth"

#: A forged token carrying an unknown `kid` must not be able to trigger a network fetch
#: per request -- that is a cheap amplification vector on a public endpoint.
_REFETCH_SECONDS = 300

_lock = threading.Lock()
_keys = None
_fetched_at = 0.0


def _load_keys():
    """The pool's signing keys.

    Read from a file baked into the deploy when one is configured, because a pool
    publishes exactly two keys and does not rotate them -- so coupling every cold start
    to a Cognito endpoint being reachable buys nothing. The network fetch is the
    fallback, not the norm.
    """
    path = getattr(settings, "COGNITO_JWKS_PATH", "")
    if path:
        raw = pathlib.Path(path)
        data = (gzip.decompress(raw.read_bytes()) if raw.suffix == ".gz"
                else raw.read_bytes())
        return {k["kid"]: k for k in json.loads(data)["keys"]}

    import urllib.request

    url = (f"https://cognito-idp.{settings.AWS_S3_REGION_NAME}.amazonaws.com/"
           f"{settings.COGNITO_POOL_ID}/.well-known/jwks.json")
    with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310
        return {k["kid"]: k for k in json.loads(response.read())["keys"]}


def signing_keys(force=False):
    global _keys, _fetched_at
    with _lock:
        stale = force and (time.monotonic() - _fetched_at) > _REFETCH_SECONDS
        if _keys is None or stale:
            _keys = _load_keys()
            _fetched_at = time.monotonic()
        return _keys


def reset_keys():
    """Drop the cached keys. Used by tests that point at a different pool."""
    global _keys, _fetched_at
    with _lock:
        _keys, _fetched_at = None, 0.0


def verify(token):
    """Validate an access token and return its claims.

    The **access** token, never the ID token: the ID token is for the client and carries
    claims that are not ours to trust server-side.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise exceptions.AuthenticationFailed("Invalid token.") from exc

    kid = header.get("kid")
    keys = signing_keys()
    if kid not in keys:
        keys = signing_keys(force=True)
    if kid not in keys:
        raise exceptions.AuthenticationFailed("Unknown signing key.")

    issuer = (f"https://cognito-idp.{settings.AWS_S3_REGION_NAME}.amazonaws.com/"
              f"{settings.COGNITO_POOL_ID}")
    try:
        claims = jwt.decode(
            token,
            key=jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(keys[kid])),
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_aud": False, "require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise exceptions.AuthenticationFailed("Invalid or expired token.") from exc

    if claims.get("token_use") != "access":
        raise exceptions.AuthenticationFailed("Wrong kind of token.")
    return claims


class CognitoCookieAuthentication(authentication.BaseAuthentication):
    """Session-style auth over a Cognito token in an httpOnly cookie.

    Enforces CSRF the same way `SessionAuthentication` did, because the credential is
    still a cookie the browser attaches automatically.

    The `client_id` check is the whole staff/customer isolation: a staff member can sign
    in through `/api/auth/login/` and be handed a customer token whose `cognito:groups`
    contains `staff`, so the staff pages must accept only tokens minted by the staff app
    client. One string comparison, and it is load-bearing.
    """

    #: Which app client's tokens this accepts.
    expected_client = "COGNITO_CUSTOMER_CLIENT_ID"
    cookie = ACCESS_COOKIE

    def authenticate(self, request):
        token = request.COOKIES.get(self.cookie)
        if not token:
            return None

        claims = verify(token)
        expected = getattr(settings, self.expected_client, "")
        if expected and claims.get("client_id") != expected:
            return None

        user = _user_from(claims)
        self.enforce_csrf(request)
        return (user, token)

    def enforce_csrf(self, request):
        """Same check `SessionAuthentication` makes, for the same reason."""
        from django.middleware.csrf import CsrfViewMiddleware

        def fail(reason):
            raise exceptions.PermissionDenied(f"CSRF Failed: {reason}")

        check = CsrfViewMiddleware(lambda req: None)
        check.process_request(request)
        reason = check.process_view(request, None, (), {})
        if reason:
            fail(reason)


def _user_from(claims):
    """Build the user from the token alone.

    `CognitoUser` fetches the profile attributes lazily, so a request that only needs
    the subject identifier -- booking, asking, the bell -- makes no Cognito call at all.
    """
    return cognito.CognitoUser.from_claims(claims)


# There is deliberately no StaffCookieAuthentication. The staff pages are server-rendered
# Django views guarded by `staff.auth.staff_required`, not DRF ones -- a DRF class here
# would imply otherwise and be the first thing somebody wired up by mistake. The staff
# cookie is verified in `staff/auth.py`, which reads STAFF_COOKIE and calls `verify`
# directly.


def set_session_cookies(response, tokens, *, secure=True):
    """Put the token bundle in httpOnly cookies.

    Both live under /api/*, which is the CachingDisabled CloudFront behaviour; the page
    behaviours forward no cookies at all, so nothing here can leak into a cached HTML
    response.
    """
    access = tokens.get("AccessToken")
    refresh_token = tokens.get("RefreshToken")
    expires = int(tokens.get("ExpiresIn") or 3600)

    if access:
        response.set_cookie(
            ACCESS_COOKIE, access, max_age=expires, httponly=True,
            secure=secure, samesite="Lax", path="/api",
        )
    if refresh_token:
        response.set_cookie(
            REFRESH_COOKIE, refresh_token, max_age=30 * 24 * 3600, httponly=True,
            secure=secure, samesite="Lax", path=REFRESH_PATH,
        )
    return response


def clear_session_cookies(response):
    response.delete_cookie(ACCESS_COOKIE, path="/api")
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH)
    return response
