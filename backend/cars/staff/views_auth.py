"""Signing staff in and out through Cognito's hosted UI."""

import logging

from django.http import HttpResponse
from django.shortcuts import redirect

from .auth import (
    clear_staff_cookie, exchange_code, groups_of, read_state, redirect_uri,
    set_staff_cookie, sign_in_url, sign_out_url, staff_required, staff_user,
)
from .permissions import may
from .. import authentication, cognito

logger = logging.getLogger(__name__)


#: Where `/api/staff/` sends somebody, in order. The first page they may read wins.
LANDING = (
    ("car.view", "staff:car-list"),
    ("booking.view", "staff:booking-list"),
    ("question.view", "staff:question-list"),
    ("customer.view", "staff:customer-list"),
    ("staff.view", "staff:staff-list"),
)


@staff_required
def index(request):
    """Where `/api/staff/` goes.

    It had no route at all, which mattered more than a missing page would suggest: it is
    the default `next` in `sign_in_url` *and* the callback's fallback destination, so
    anyone arriving without a signed `state` landed on a 404 the moment they finished
    authenticating -- the one point where a person is least able to tell a broken deploy
    from a rejected login.

    Resolved against what this person may actually read rather than sending everyone to
    a fixed page, because group membership decides that: an owner and an inventory
    manager differ, and a new starter in no group may read none of them. Bouncing that
    last case into a 403 would look identical to a login failure, so it gets a sentence
    instead.
    """
    groups = groups_of(request.staff)
    for action, route in LANDING:
        if may(groups, action):
            return redirect(route)
    return HttpResponse(
        "Signed in, but this account has no role yet, so there is nothing to show. "
        "An owner can grant one at /api/staff/accounts/.",
        status=200, content_type="text/plain; charset=utf-8")


def sign_in(request):
    """Send them to Cognito, carrying where they were trying to go."""
    return redirect(sign_in_url(request.GET.get("next") or "/api/staff/"))


def callback(request):
    """Back from Cognito with a code.

    Everything is re-checked here rather than trusted because it came from a redirect:
    the state's signature, the token's own signature, which app client minted it, and
    whether the person is actually staff.
    """
    code = request.GET.get("code") or ""
    if not code:
        return redirect(sign_in_url())

    destination = read_state(request.GET.get("state")) or "/api/staff/"

    try:
        # `redirect_uri()`, never the request path: APPEND_SLASH may have already
        # rewritten it, and the exchange must match the authorize call exactly.
        tokens = exchange_code(code, redirect_uri())
    except Exception:  # noqa: BLE001 - a bad or reused code is not an error page
        logger.warning("staff token exchange failed", exc_info=True)
        return redirect(sign_in_url(destination))

    access = tokens.get("access_token")
    try:
        claims = authentication.verify(access) if access else None
    except Exception:  # noqa: BLE001
        claims = None
    if claims is None:
        return redirect(sign_in_url(destination))

    user = cognito.CognitoUser.from_claims(claims)
    if not user.is_staff:
        # A genuine customer token, or somebody who has been removed from the group.
        return HttpResponse(
            "This account does not have staff access.", status=403,
            content_type="text/plain; charset=utf-8")

    response = redirect(destination)
    return set_staff_cookie(response, tokens, secure=request.is_secure())


def sign_out(request):
    """Clear the cookie here, then let Cognito clear its own session."""
    user = staff_user(request)
    if user is not None and hasattr(user, "sub"):
        cognito.sign_out(user.sub)
    response = redirect(sign_out_url())
    return clear_staff_cookie(response)


def signed_out(request):
    """Where Cognito returns after its logout endpoint."""
    return HttpResponse(
        "Signed out.", status=200, content_type="text/plain; charset=utf-8")


def not_configured(request):
    """No hosted UI, so there is no way to sign in.

    This used to redirect to the Django admin's login form, which was a real fallback
    while both auth systems existed. There is no second way in now, so saying so beats
    a redirect to a URL that 404s -- and 503 is the honest code: the deploy is missing
    a setting, not the visitor a permission.
    """
    return HttpResponse(
        "Staff sign-in is not configured: COGNITO_DOMAIN is unset on this deploy.",
        status=503, content_type="text/plain; charset=utf-8")
