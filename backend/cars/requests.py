"""The rules for asking the shop to find a car.

Thin views, as everywhere: the checks live here, so the same request is refused for
the same reason from the app, from curl and from a test.

Anyone may ask. That is the point of the page -- a person who has not bought anything
yet and has no account is exactly who is looking for a car -- so the endpoint is open
and the throttle does the work a sign-in would. A guest gives a name, an address and a
number, because a wish with no way to reply to it is a wish nobody can act on. A
signed-in customer gives nothing but the wish: their details are on file, and they are
copied onto the request here rather than looked up later, so it still reads whole if
the account changes or goes.
"""

from django.utils import timezone

from . import identity, mail
from .store import requests as store

#: Room for a paragraph or three. It is a description of a car, not an essay, and the
#: staff page and the email both print it whole.
MAX_DETAILS_LENGTH = 2000

MAX_NAME_LENGTH = 120
MAX_CONTACT_LENGTH = 120


class RequestError(Exception):
    """Something the person can read and act on."""


def submit(*, user, name="", email="", phone="", details="", language="en"):
    """Record a request and tell the shop. Returns the stored request."""
    text = (details or "").strip()
    if not text:
        raise RequestError("Tell us about the car you are looking for first.")
    if len(text) > MAX_DETAILS_LENGTH:
        raise RequestError(
            f"Please keep it under {MAX_DETAILS_LENGTH} characters. If there is a lot "
            "to say, leave the essentials here and we will call you."
        )

    sub = identity.sub_of(user) if user is not None else None
    if sub:
        name = identity.full_name_of(user) or name
        email = identity.email_of(user) or email
        phone = identity.phone_of(user) or phone

    name, email, phone = [(v or "").strip() for v in (name, email, phone)]
    if not (name and email and phone):
        raise RequestError(
            "We need a name, an email address and a phone number so we can get back "
            "to you."
        )
    if len(name) > MAX_NAME_LENGTH or len(email) > MAX_CONTACT_LENGTH \
            or len(phone) > MAX_CONTACT_LENGTH:
        raise RequestError("That name or contact detail is too long.")
    if "@" not in email:
        raise RequestError("That email address does not look right.")

    record = store.create(
        name=name, email=email, phone=phone, details=text,
        language="ja" if language == "ja" else "en",
        customer_sub=sub, now=timezone.now(),
    )
    mail.notify_staff_of_request(record)
    return record


def resolve(request, *, staff=None, note="", now=None):
    return store.resolve(request, staff_sub=identity.sub_of(staff) if staff else "",
                         note=note, now=now or timezone.now())


def reopen(request, *, now=None):
    return store.reopen(request, now=now or timezone.now())
