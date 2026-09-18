"""Signing a test in, with neither Cognito nor Django's auth tables.

`force_login` went with `django.contrib.auth`, and most of the suite does not care how
somebody signed in -- a booking test needs *a customer*, not a round trip
through a token endpoint. The high-fidelity path still exists and is still exercised:
`tests_cognito.py` and `tests_staff_auth.py` mint real RS256 tokens against moto and
verify them for real. That is where the signature, the `client_id` check and the group
mapping are pinned down. Everything else uses this.

Deliberately not moto for the other ~150: moto is an optional dependency, so those tests
would be `skipUnless`-gated and would silently vanish on any machine without
`requirements-dev.txt`. A suite that disappears when a dependency is missing is worse
than one that is a little less faithful.

**Nothing in production knows this exists.** Both cookie flows -- DRF's
`CognitoCookieAuthentication` for the API and `staff.auth.staff_user` for the
server-rendered pages -- resolve `cars.authentication.verify` at call time, so patching
that one name covers both. The second patch stands in for the directory
`CognitoUser` would otherwise fetch attributes from. No branch anywhere asks whether it
is being tested.
"""

import itertools
import uuid
from unittest import mock

from django.conf import settings

from . import authentication, cognito

#: sub -> the attribute dict `cognito.attributes_of` would have returned.
_DIRECTORY = {}
#: opaque cookie value -> the claims `verify` would have decoded.
_TOKENS = {}

_counter = itertools.count(1)


def reset():
    _DIRECTORY.clear()
    _TOKENS.clear()


def _attributes_of(sub):
    """None for somebody who is not there, which is what the real one returns.

    `CognitoUser._load` turns that into an empty attribute set, so a question whose
    asker has closed their account still renders -- it just has nobody to name.
    """
    return _DIRECTORY.get(sub)


def close_account(user):
    """Forget somebody, the way `AdminDeleteUser` would.

    The store keeps their subject identifier on every item that referenced them, which
    is the property worth testing: a closed account must not take published page content
    with it.
    """
    _DIRECTORY.pop(user.sub, None)


def _verify(token):
    """Stand in for `authentication.verify`.

    Fails the same way the real one does, so a test asserting that a bad cookie is
    turned away is asserting the real behaviour and not this module's.
    """
    claims = _TOKENS.get(token)
    if claims is None:
        from rest_framework import exceptions

        raise exceptions.AuthenticationFailed("Invalid token.")
    return claims


def make_user(email=None, *, name="", phone="", groups=(), sub=None, is_active=True):
    """Register somebody and return the `CognitoUser` the app would see.

    The attributes go in the directory rather than onto the object, so `CognitoUser`
    loads them through exactly the path it uses in production -- which is what keeps a
    lazily-fetched attribute honest here.
    """
    nth = next(_counter)
    sub = sub or str(uuid.uuid5(uuid.NAMESPACE_URL, f"test-sub-{nth}"))
    email = email or f"person{nth}@example.com"

    first, _, last = (name or "").partition(" ")
    _DIRECTORY[sub] = {
        "email": email,
        "given_name": first,
        "family_name": last,
        cognito.PHONE_ATTR: phone,
    }
    return cognito.CognitoUser(sub=sub, groups=tuple(groups), is_active=is_active)


def token_for(user, *, staff=False):
    """A cookie value that `_verify` will resolve back to this person.

    Carries the `client_id` the real token would carry, so the staff/customer isolation
    check in `CognitoCookieAuthentication` and `staff.auth.staff_user` is exercised
    rather than sidestepped.
    """
    setting = "COGNITO_STAFF_CLIENT_ID" if staff else "COGNITO_CUSTOMER_CLIENT_ID"
    token = f"fake-{'staff' if staff else 'customer'}-{user.sub}"
    _TOKENS[token] = {
        "sub": user.sub,
        "cognito:groups": list(user.groups),
        "client_id": getattr(settings, setting, "") or f"test-{setting.lower()}",
        "token_use": "access",
    }
    return token


def sign_in(client, user, *, staff=False):
    """Put the right cookie on the test client. The replacement for `force_login`."""
    cookie = authentication.STAFF_COOKIE if staff else authentication.ACCESS_COOKIE
    client.cookies[cookie] = token_for(user, staff=staff)
    return user


def sign_out(client, *, staff=False):
    cookie = authentication.STAFF_COOKIE if staff else authentication.ACCESS_COOKIE
    client.cookies.pop(cookie, None)


class FakeCognito:
    """Mixin. Installs the stand-in for the duration of each test."""

    def setUp(self):
        reset()
        for target, name, replacement in (
            (authentication, "verify", _verify),
            (cognito, "attributes_of", _attributes_of),
        ):
            patcher = mock.patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()
