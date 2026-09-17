"""Signing staff in and out through Cognito's hosted UI."""

import logging

from django.http import HttpResponse
from django.shortcuts import redirect

from .auth import (
    clear_staff_cookie, exchange_code, read_state, set_staff_cookie, sign_in_url,
    sign_out_url, staff_user,
)
from .. import authentication, cognito

logger = logging.getLogger(__name__)


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
        tokens = exchange_code(code, request.build_absolute_uri(request.path))
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
