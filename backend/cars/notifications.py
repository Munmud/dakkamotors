"""Writing to a customer's bell.

One entry point, `notify()`, so every notification in the system is created the same way
and there is one place to look when one is missing.

**This module never sends email.** Booking confirmations and cancellations already send
their own, carrying the address, the licence reminder and the cancel link; a second
message saying the same thing in fewer words would be spam. The one case that does email
is a question being answered, and that lives in `qa.record_answer` next to the answer
itself - not here.

Storage moved to DynamoDB; the rules did not. Two things changed underneath:

* `notify()` used to be called *inside* a database transaction so the bell entry rolled
  back with the event that caused it. It now writes the notification and its dedupe
  guard in a single `TransactWriteItems`, which is a stronger guarantee than a
  session-scoped one -- but it means the caller must no longer rely on an enclosing
  `atomic()` block to undo it. Where a bell entry must live or die with a booking, put
  both in the same store transaction instead.
* The trim-on-write dance is mostly gone. Rows carry a TTL, so expiry is DynamoDB's job.
  The old comment explained the dance existed because "a scheduled cleanup would wake
  Aurora around the clock"; that constraint is gone and does not need solving in Python.
"""

from django.utils import timezone

from .identity import is_reachable, sub_of
from .store import notifications as store


def notify(*, user, kind, context=None, dedupe_key="", now=None):
    """Put one thing in a customer's bell. Returns the row, or None if there is nobody.

    Idempotent when given a dedupe_key: a staff action and a save can both reach the
    same domain function for one real-world event, and a customer should not be told
    twice.
    """
    if not is_reachable(user):
        # Staff-seeded questions have no asker, and a closed account has no bell.
        return None

    row, _created = store.notify(
        customer_sub=sub_of(user),
        kind=kind,
        context=context or {},
        dedupe_key=dedupe_key,
        now=now or timezone.now(),
    )
    return row


def unread_count(user):
    if not is_reachable(user):
        return 0
    return store.unread_count(sub_of(user))


def recent_for(user, limit=20):
    if not is_reachable(user):
        return []
    return store.recent(sub_of(user), limit=limit)


def mark_read(user, ids=None, now=None):
    """Mark everything, or just the given ids. Returns the remaining unread count.

    Always scoped to the requesting customer -- the sub is part of the partition key, so
    another person's ids match nothing rather than marking their notifications read.
    That used to be a `filter(customer=user)` someone could forget; now it is structural.
    """
    if not is_reachable(user):
        return 0
    sub = sub_of(user)
    store.mark_read(sub, ids=ids, now=now or timezone.now())
    return store.unread_count(sub)
