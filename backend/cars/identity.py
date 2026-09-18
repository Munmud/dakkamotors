"""Reading a person, safely, wherever one is handed in.

This began as a bridge between Django users and Cognito ones and was meant to be deleted
with the ORM. Most of it earned its place instead: `is_reachable`, `email_of` and
`full_name_of` are null-safe accessors used at twenty-nine call sites, and inlining
`(getattr(user, "email", "") or "").strip()` at each of them would be worse code, not
less of it. What went is the branching -- the integer-primary-key lookups and the
`customer_profile` hop.

The one thing to keep in mind: a user may legitimately be `None`. A staff-seeded question
has no asker and a closed account has no bell, and both are ordinary states rather than
errors, which is why nothing here raises.
"""


def sub_of(user):
    """The store's identifier for a customer, or None if there is nobody."""
    if user is None:
        return None
    sub = getattr(user, "sub", None)
    return str(sub) if sub else None


def is_reachable(user):
    """Whether this person can receive anything at all.

    Staff-seeded questions have no asker, and a deactivated account has no bell. Both
    are ordinary states, not errors, so callers check rather than guard with try.
    """
    return user is not None and bool(getattr(user, "is_active", False))


def email_of(user):
    return (getattr(user, "email", "") or "").strip()


def phone_of(user):
    """The phone number, or empty.

    `custom:phone` on the Cognito user, not the standard `phone_number`: Cognito
    validates that one as E.164 and customers here type `080-9282-3601`.
    """
    return getattr(user, "phone", "") or ""


def full_name_of(user):
    if user is None:
        return ""
    getter = getattr(user, "get_full_name", None)
    name = getter() if callable(getter) else ""
    return (name or getattr(user, "username", "") or "").strip()


def car_id_of(car):
    """The store's identifier for a car, or None."""
    if car is None:
        return None
    car_id = getattr(car, "car_id", None)
    return str(car_id) if car_id else None


def user_for_sub(sub):
    """Resolve a stored subject identifier back to a person, or None.

    The other half of `sub_of`, and one `AdminGetUser` call.

    Returns None rather than raising: a question outlives the account that asked it by
    design -- it holds a subject identifier, not a foreign key, so a closed account
    cannot take published page content with it -- and "nobody to tell" is an ordinary
    answer rather than a failure.
    """
    if not sub:
        return None

    from . import cognito

    return cognito.user_for(sub)
