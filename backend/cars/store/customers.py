"""Customer storage -- the little of it that is not Cognito's job.

Name, email and phone live on the Cognito user. This item exists for one reason:
`active_bookings` has to be incremented in the same transaction as the booking, and a
Cognito attribute cannot participate in a DynamoDB transaction.

It also gives the staff customer list something to read that is not a paginated
`ListUsers` call against a remote API.
"""

import datetime as dt

from . import keys
from .errors import NotFound
from .models import Customer


def ensure(*, sub, email, now=None):
    """Create the counter item if this customer has never had one.

    Conditional, so two concurrent first-bookings cannot race and reset the counter to
    zero. Returns the item either way.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    customer = Customer(
        pk=keys.customer_pk(sub),
        sk=keys.PROFILE,
        customer_sub=sub,
        email=(email or "").lower(),
        active_bookings=0,
        created_at=now,
        gsi1pk=keys.CUSTOMER_GSI1PK,
        gsi1sk=(email or "").lower(),
    )
    try:
        customer.save(condition=Customer.pk.does_not_exist())
        return customer
    except Exception as exc:
        if not _is_conditional_failure(exc):
            raise
    return get(sub)


def _is_conditional_failure(exc):
    cause = getattr(exc, "cause", None)
    code = (cause.response.get("Error", {}).get("Code")
            if cause is not None and hasattr(cause, "response") else None)
    return code == "ConditionalCheckFailedException"


def get(sub):
    try:
        return Customer.get(keys.customer_pk(sub), keys.PROFILE)
    except Customer.DoesNotExist:
        raise NotFound(f"no customer {sub!r}") from None


def find(sub):
    try:
        return get(sub)
    except NotFound:
        return None


def set_email(sub, email):
    """Keep the staff list's sort key in step when Cognito's email changes.

    In practice email is locked -- `auth_views.LOCKED_PROFILE_FIELDS` exists because the
    username and the email have to move together or a password reset becomes a coin toss
    -- so this is for staff-initiated corrections only.
    """
    customer = get(sub)
    lowered = (email or "").lower()
    customer.update(actions=[
        Customer.email.set(lowered),
        Customer.gsi1sk.set(lowered),
    ])
    return customer


def all_customers(limit=None):
    """The staff customer list, alphabetical by email."""
    rows = list(Customer.gsi1.query(keys.CUSTOMER_GSI1PK))
    return rows[:limit] if limit else rows


def recount(sub, active_bookings):
    """Set a customer's active-booking counter to a known value.

    For repair after a restore or a partial import, never on a request path -- the
    counter is maintained transactionally by `bookings.py`, and setting it outside a
    transaction is exactly the race that design avoids.

    Nothing calls this today: `manage.py reconcile_counters` writes the attribute
    directly. Kept because a counter you cannot reset by hand is worse than an unused
    function, and because the next repair script should use this rather than reinvent it.
    """
    customer = get(sub)
    customer.update(actions=[Customer.active_bookings.set(int(active_bookings))])
    return customer
