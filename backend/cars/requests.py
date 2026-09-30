"""The rules for a lead: somebody telling the shop they are interested.

One shape covers both cases the site has. A lead **with a car** is "I want this one,
call me" — the whole job of a car page. A lead **without one** is "I am looking for
something you do not have", which is the same person one step earlier. They differ by a
single optional field, so they are one item, one queue and one email rather than two
near-identical features.

Thin views, as everywhere: the checks live here, so the same lead is refused for the
same reason from the app, from curl and from a test.

**Name and phone, and that is all we insist on.** Email is optional because the shop
rings people — a form asking for three things converts at roughly a quarter, one asking
for more at a tenth, and every field is a reason to close the tab. A message is optional
too when a car is named: clicking "call me back" on a specific car has already said
everything a message would.

Anyone may send one. There are no accounts; the throttle does the work a sign-in used
to.
"""

from django.utils import timezone

from . import advertising, mail
from .store import requests as store

#: Room for a paragraph or three. It is a description of a car, not an essay, and the
#: staff page and the email both print it whole.
MAX_DETAILS_LENGTH = 2000

MAX_NAME_LENGTH = 120
MAX_CONTACT_LENGTH = 120


#: Every sentence a person can be refused with, in both languages.
#:
#: This project has said since the beginning that the domain module owns every
#: sentence a customer reads, and that customer-facing messages are bilingual off a
#: stored language. The lead path was the one place that was not -- and it is now the
#: only path there is, so a Japanese visitor filling the only form on the site could
#: be refused in English on a page that is otherwise entirely in Japanese.
MESSAGES = {
    "need_wish": {
        "en": "Tell us about the car you are looking for first.",
        "ja": "お探しのお車について、先にご記入ください。",
    },
    "too_long": {
        "en": (f"Please keep it under {MAX_DETAILS_LENGTH} characters. If there is a "
               "lot to say, leave the essentials here and we will call you."),
        "ja": (f"{MAX_DETAILS_LENGTH}文字以内でご記入ください。"
               "詳しいお話は、お電話でお伺いします。"),
    },
    "need_contact": {
        "en": "We need a name and a phone number so we can call you back.",
        "ja": "折り返しご連絡いたしますので、お名前とお電話番号をご記入ください。",
    },
    "too_long_contact": {
        "en": "That name or contact detail is too long.",
        "ja": "お名前または連絡先が長すぎます。",
    },
    "bad_email": {
        "en": "That email address does not look right.",
        "ja": "メールアドレスをご確認ください。",
    },
    "no_such_car": {
        "en": "We could not find that car.",
        "ja": "そのお車が見つかりませんでした。",
    },
}


def message(key, language="en"):
    """One refusal, in the language the person is reading."""
    return MESSAGES[key]["ja" if language == "ja" else "en"]


class RequestError(Exception):
    """Something the person can read and act on, in their own language.

    Carries the key as well as the rendered text, so a caller that wants the other
    language -- a test, or a view that resolved the reader later than this did -- can
    ask for it rather than string-matching English.
    """

    def __init__(self, key, language="en"):
        self.key = key
        self.language = language
        super().__init__(message(key, language))


def submit(*, name="", email="", phone="", details="", car=None, language="en"):
    """Record a lead and tell the shop. Returns the stored lead.

    `car` is the store's Car, or None. When there is one the message may be empty; when
    there is not, the message *is* the lead and there is nothing to act on without it.
    """
    lang = "ja" if language == "ja" else "en"

    text = (details or "").strip()
    if not text and car is None:
        raise RequestError("need_wish", lang)
    if len(text) > MAX_DETAILS_LENGTH:
        raise RequestError("too_long", lang)

    name, email, phone = [(v or "").strip() for v in (name, email, phone)]
    if not (name and phone):
        raise RequestError("need_contact", lang)
    if len(name) > MAX_NAME_LENGTH or len(email) > MAX_CONTACT_LENGTH \
            or len(phone) > MAX_CONTACT_LENGTH:
        raise RequestError("too_long_contact", lang)
    # Only when they gave one. A bare "@" check rather than a validator, deliberately:
    # the address is a convenience here, not the way the shop replies, and refusing a
    # valid-but-unusual address would cost a lead to gain nothing.
    if email and "@" not in email:
        raise RequestError("bad_email", lang)

    record = store.create(
        name=name, email=email, phone=phone, details=text, car=car,
        language=lang,
        now=timezone.now(),
    )
    mail.notify_staff_of_request(record)

    # This car has now done what the advertisement was paid to make it do, so the ad
    # set stops. Fire-and-forget, exactly as it was on the booking path: a lead must
    # never fail because Meta was slow, which would lose the very thing the money
    # bought. See cars/advertising.py.
    advertising.stop_for(record)
    return record


def resolve(request, *, staff=None, note="", now=None):
    # `sub` straight off the Cognito user. This went through `identity.sub_of` while
    # that module bridged Django users and Cognito ones; the bridge outlived its last
    # caller when customer accounts went.
    return store.resolve(request, staff_sub=getattr(staff, "sub", "") or "",
                         note=note, now=now or timezone.now())


def reopen(request, *, now=None):
    return store.reopen(request, now=now or timezone.now())
