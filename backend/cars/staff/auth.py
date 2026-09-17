"""Who may reach the staff pages.

Staff arrive through Cognito's hosted UI: a redirect to `/oauth2/authorize`, a code back
to `/api/staff/auth/callback`, exchanged for tokens that are verified and stored in the
`dm_st` cookie. That buys TOTP MFA on the accounts that can edit inventory and other
staff, which is a real upgrade on a plain Django password, and it is three people
redirecting rather than a whole customer journey being moved off-site.

**The client_id check is the whole staff/customer isolation.** A staff member can sign in
through `/api/auth/login/` like anybody else and be handed a customer token whose
`cognito:groups` contains `staff`. These pages accept only tokens minted by the *staff*
app client, which the customer client cannot produce. One string comparison, and
everything rests on it.

While both auth systems exist, a Django staff session is still accepted. That is what
keeps the existing staff-page tests meaningful and lets the two land separately; it goes
with `django.contrib.auth`.
"""

import base64
import hashlib
import hmac
import json
import logging
import urllib.parse
import urllib.request
from functools import wraps

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from .. import authentication, cognito
from .permissions import may

logger = logging.getLogger(__name__)

STAFF_COOKIE = authentication.STAFF_COOKIE
STAFF_PATH = "/api/staff"


# --------------------------------------------------------------------------------------
# Who is asking
# --------------------------------------------------------------------------------------

def staff_user(request):
    """The signed-in staff member, or None.

    Tries the Cognito cookie first, then a Django staff session. Both are cookie
    credentials carrying the same weight; the second is transitional.
    """
    token = request.COOKIES.get(STAFF_COOKIE)
    if token:
        try:
            claims = authentication.verify(token)
        except Exception:  # noqa: BLE001 - an unreadable cookie is simply not signed in
            return None
        expected = settings.COGNITO_STAFF_CLIENT_ID
        if expected and claims.get("client_id") != expected:
            # A customer token, however genuine. Not a credential for these pages.
            return None
        user = cognito.CognitoUser.from_claims(claims)
        return user if user.is_staff else None

    django_user = getattr(request, "user", None)
    if getattr(django_user, "is_authenticated", False) and django_user.is_staff:
        return django_user
    return None


def groups_of(user):
    """The Cognito groups this person is in, mapped from Django's flags if need be."""
    groups = getattr(user, "groups", None)
    if isinstance(groups, (list, tuple)):
        return set(groups)

    # A Django user during the transition. Its group names are the same strings.
    names = set(user.groups.values_list("name", flat=True)) if groups is not None else set()
    mapped = set()
    if getattr(user, "is_superuser", False):
        mapped.add(cognito.OWNERS_GROUP)
    if "Inventory Managers" in names:
        mapped.add(cognito.INVENTORY_GROUP)
    return mapped


# --------------------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------------------

def staff_required(view):
    """Signed in, and staff.

    Redirects rather than 403ing, so somebody following a link from an email lands
    somewhere useful.
    """
    @wraps(view)
    def guarded(request, *args, **kwargs):
        user = staff_user(request)
        if user is None:
            return redirect(sign_in_url(request.get_full_path()))
        request.staff = user
        return view(request, *args, **kwargs)
    return guarded


def requires(action):
    """Guard one action behind a group.

    Written as an explicit action string rather than derived from a model, so what a
    role may do stays readable in `permissions.py` instead of being reconstructed from
    an app label.
    """
    def decorate(view):
        @wraps(view)
        def guarded(request, *args, **kwargs):
            user = getattr(request, "staff", None) or staff_user(request)
            if user is None:
                return redirect(sign_in_url(request.get_full_path()))
            if not may(groups_of(user), action):
                raise PermissionDenied
            request.staff = user
            return view(request, *args, **kwargs)
        return guarded
    return decorate


# --------------------------------------------------------------------------------------
# The hosted UI round trip
# --------------------------------------------------------------------------------------

def _domain():
    return settings.COGNITO_DOMAIN


def _redirect_uri(request):
    return request.build_absolute_uri("/api/staff/auth/callback")


def sign_state(next_path):
    """A signed `state`, carrying where to go afterwards.

    Signed rather than stored so the callback needs no session, and signed rather than
    plain so the destination cannot be swapped for somebody else's.
    """
    payload = base64.urlsafe_b64encode(
        json.dumps({"next": next_path}).encode("utf-8")).decode("ascii").rstrip("=")
    mac = hmac.new(settings.SECRET_KEY.encode("utf-8"),
                   payload.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{mac}"


def read_state(state):
    """The destination a `state` carries, or "" if it was not one of ours."""
    try:
        payload, mac = (state or "").split(".", 1)
    except ValueError:
        return ""
    expected = hmac.new(settings.SECRET_KEY.encode("utf-8"),
                        payload.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(mac, expected):
        return ""
    try:
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:  # noqa: BLE001
        return ""
    nxt = data.get("next") or ""
    # Same rule as `auth_views.safe_next`: a single leading slash, nothing else.
    if not nxt.startswith("/") or nxt.startswith("//"):
        return ""
    return nxt[:200]


def sign_in_url(next_path="/api/staff/"):
    if not _domain():
        # No hosted UI configured yet. The Django admin login still works and is what
        # the staff pages fall back to while both systems exist.
        return "/api/admin/login/?next=" + urllib.parse.quote(next_path)

    query = urllib.parse.urlencode({
        "client_id": settings.COGNITO_STAFF_CLIENT_ID,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": f"https://{settings.ALLOWED_HOSTS[0]}/api/staff/auth/callback",
        "state": sign_state(next_path),
    })
    return f"https://{_domain()}/oauth2/authorize?{query}"


def sign_out_url():
    if not _domain():
        return "/api/admin/logout/"
    query = urllib.parse.urlencode({
        "client_id": settings.COGNITO_STAFF_CLIENT_ID,
        "logout_uri": f"https://{settings.ALLOWED_HOSTS[0]}/api/staff/signed-out",
    })
    return f"https://{_domain()}/logout?{query}"


def exchange_code(code, redirect_uri):
    """Swap an authorization code for tokens at the hosted UI's token endpoint.

    **The one thing here that no local stand-in covers.** moto does not implement
    Cognito's `/oauth2/token`, so this function is exercised against the real service or
    not at all -- which is why it is a single function with no logic around it, and why
    everything that *can* be tested (the state signing, the client_id check, the group
    map, the cookie handling) lives outside it.
    """
    secret = getattr(settings, "COGNITO_STAFF_CLIENT_SECRET", "")
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": settings.COGNITO_STAFF_CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode("utf-8")

    request = urllib.request.Request(
        f"https://{_domain()}/oauth2/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    if secret:
        pair = f"{settings.COGNITO_STAFF_CLIENT_ID}:{secret}".encode("utf-8")
        request.add_header(
            "Authorization", "Basic " + base64.b64encode(pair).decode("ascii"))

    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
        return json.loads(response.read())


def set_staff_cookie(response, tokens, *, secure=True):
    access = tokens.get("access_token") or tokens.get("AccessToken")
    if not access:
        return response
    response.set_cookie(
        STAFF_COOKIE, access, max_age=int(tokens.get("expires_in") or 1800),
        httponly=True, secure=secure, samesite="Lax", path=STAFF_PATH)
    return response


def clear_staff_cookie(response):
    response.delete_cookie(STAFF_COOKIE, path=STAFF_PATH)
    return response
