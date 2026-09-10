"""Writing to a customer's bell.

One entry point, `notify()`, so every notification in the system is created the same way
and there is one place to look when one is missing.

**This module never sends email.** Booking confirmations and cancellations already send
their own, carrying the address, the licence reminder and the cancel link; a second
message saying the same thing in fewer words would be spam. The one case that does email
is a question being answered, and that lives in `qa.record_answer` next to the answer
itself - not here.

Call `notify()` *inside* the transaction that caused the event, so the bell entry rolls
back with it. Leave the email in `transaction.on_commit`, so a rollback cannot send a
message about something that never happened.
"""

import datetime as dt

from django.utils import timezone

from .notification_models import Notification

# How much history a customer keeps. Two people over a year of bookings is a handful of
# rows; the cap exists so a pathological case cannot grow without bound.
KEEP_READ_DAYS = 90
KEEP_READ_ROWS = 50


def notify(*, user, kind, context=None, dedupe_key="", now=None):
    """Put one thing in a customer's bell. Returns the row, or None if there is nobody.

    Idempotent when given a dedupe_key: an admin action and a save_model can both reach
    the same domain function, and a customer should not be told twice.
    """
    if user is None or not getattr(user, "is_active", False):
        # Staff-seeded questions have no asker, and a closed account has no bell.
        return None

    now = now or timezone.now()
    # created_at is auto_now_add, so it is not passed here - it would be ignored, and a
    # parameter that silently does nothing is worse than no parameter. `now` is used for
    # the trim window instead.
    defaults = {"kind": kind, "context": context or {}}

    if dedupe_key:
        row, _ = Notification.objects.get_or_create(
            customer=user, dedupe_key=dedupe_key, defaults=defaults
        )
    else:
        row = Notification.objects.create(customer=user, dedupe_key="", **defaults)

    _trim(user, now)
    return row


def _trim(user, now):
    """Bound the table on write, because nothing may sweep it on a schedule.

    Aurora is set to scale to zero; a nightly cleanup job would wake it every night to
    delete almost nothing, which costs more than the rows do. So this runs where the
    table is already being written - the same reasoning as `booking.ensure_slots` and the
    expired-registration sweep in `RegisterView`.

    Unread rows are never touched. Losing something a customer has not seen is exactly
    the failure this feature exists to prevent.
    """
    read = Notification.objects.filter(customer=user, read_at__isnull=False)

    stale = read.filter(created_at__lt=now - dt.timedelta(days=KEEP_READ_DAYS))
    stale.delete()

    keep = list(
        read.order_by("-created_at").values_list("pk", flat=True)[:KEEP_READ_ROWS]
    )
    read.exclude(pk__in=keep).delete()


def unread_count(user):
    return Notification.objects.filter(customer=user, read_at__isnull=True).count()


def recent_for(user, limit=20):
    return list(Notification.objects.filter(customer=user)[:limit])


def mark_read(user, ids=None, now=None):
    """Mark everything, or just the given ids. Returns the remaining unread count.

    Always scoped to the requesting customer, so another person's ids match nothing
    rather than marking their notifications read.
    """
    now = now or timezone.now()
    rows = Notification.objects.filter(customer=user, read_at__isnull=True)
    if ids is not None:
        rows = rows.filter(pk__in=ids)
    rows.update(read_at=now)
    return unread_count(user)
