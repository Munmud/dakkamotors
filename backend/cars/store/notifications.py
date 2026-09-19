"""The customer's bell.

Still no stored text, for the reason `notification_models.py` gives: the site has a
language switcher, so a customer who signs up in English and later reads in Japanese must
not find their history frozen. What is stored is the `kind` and enough `context` to
render it at read time.

Two things get simpler here. The `one_notification_per_event` partial unique becomes a
guard item, skipped entirely when the dedupe key is blank -- which is both what the old
`condition=~Q(dedupe_key="")` meant and how we avoid DynamoDB's refusal of an empty key
attribute. And the trim-on-write dance disappears into a TTL: the old comment explains it
existed because "a scheduled cleanup would wake Aurora around the clock", which is no
longer a constraint and no longer needs solving in Python.
"""

import datetime as dt

from . import keys
from .models import DedupeGuard, Notification
from .txn import Txn, failed

#: How long a bell entry survives. Expired rows are removed by DynamoDB, free.
KEEP_DAYS = 120

#: Belt and braces on top of the TTL -- a very chatty account should not accumulate an
#: unbounded feed between expiries.
KEEP_ROWS = 50

NOTIF, GUARD = "notification", "guard"


def notify(*, customer_sub, kind, context, dedupe_key="", now):
    """Write one bell entry. Returns (notification, created).

    Idempotent when a dedupe key is given, which matters because a staff action and a
    save can both reach the same domain function for one real-world event.
    """
    notification_id = keys.new_id()
    sk = keys.notification_sk(now, notification_id)
    ttl = int((now + dt.timedelta(days=KEEP_DAYS)).timestamp())

    row = Notification(
        pk=keys.customer_pk(customer_sub),
        sk=sk,
        notification_id=notification_id,
        customer_sub=customer_sub,
        kind=kind,
        context=context or {},
        dedupe_key=dedupe_key or "",
        created_at=now,
        ttl=ttl,
    )

    if not dedupe_key:
        row.save()
        _trim(customer_sub)
        return row, False

    tx = Txn()
    tx.save(NOTIF, row)
    tx.save(GUARD,
            DedupeGuard(pk=keys.customer_pk(customer_sub),
                        sk=keys.dedupe_sk(dedupe_key),
                        notification_sk=sk, ttl=ttl),
            condition=DedupeGuard.sk.does_not_exist())
    order = tx.labels_in_wire_order()
    try:
        tx.commit()
    except Exception as exc:
        if getattr(exc, "reasons", None) is not None and failed(exc, GUARD, order):
            return _existing(customer_sub, dedupe_key), False
        raise

    _trim(customer_sub)
    return row, True


def build(*, customer_sub, kind, context, dedupe_key="", now):
    """Prepare a bell entry and its guard without writing them.

    Lets a caller put the notification into *their* transaction, so the entry and the
    event that caused it are one atomic write. That is what makes "a confirmation that
    did not happen leaves no notification" a property of the storage layer rather than
    of an enclosing session -- the guarantee the Django version had, and the reason
    `notify()` used to insist on being called inside the transaction.

    Returns (notification, guard-or-None).
    """
    notification_id = keys.new_id()
    sk = keys.notification_sk(now, notification_id)
    ttl = int((now + dt.timedelta(days=KEEP_DAYS)).timestamp())

    row = Notification(
        pk=keys.customer_pk(customer_sub),
        sk=sk,
        notification_id=notification_id,
        customer_sub=customer_sub,
        kind=kind,
        context=context or {},
        dedupe_key=dedupe_key or "",
        created_at=now,
        ttl=ttl,
    )
    if not dedupe_key:
        return row, None
    guard = DedupeGuard(
        pk=keys.customer_pk(customer_sub),
        sk=keys.dedupe_sk(dedupe_key),
        notification_sk=sk,
        ttl=ttl,
    )
    return row, guard


def _existing(customer_sub, dedupe_key):
    """The notification a dedupe guard already points at.

    Matches `get_or_create`'s contract: the caller gets the row that won, not None.
    """
    try:
        guard = DedupeGuard.get(keys.customer_pk(customer_sub),
                                keys.dedupe_sk(dedupe_key))
    except DedupeGuard.DoesNotExist:
        return None
    try:
        return Notification.get(keys.customer_pk(customer_sub), guard.notification_sk)
    except Notification.DoesNotExist:
        return None


def recent(customer_sub, limit=20):
    """The feed, newest first."""
    rows = Notification.query(
        keys.customer_pk(customer_sub),
        range_key_condition=Notification.sk.startswith("NOTIF#"),
        scan_index_forward=False,
    )
    out = []
    for row in rows:
        out.append(row)
        if len(out) >= limit:
            break
    return out


def unread_count(customer_sub):
    """Counted from the same page the feed reads.

    The old code issued a separate `COUNT(*)`; here the feed query already has the rows,
    so the bell badge costs nothing extra.
    """
    return sum(1 for row in recent(customer_sub, limit=KEEP_ROWS)
               if row.read_at is None)


def mark_read(customer_sub, ids=None, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    wanted = set(ids) if ids else None
    touched = 0
    for row in recent(customer_sub, limit=KEEP_ROWS):
        if row.read_at is not None:
            continue
        if wanted is not None and row.notification_id not in wanted:
            continue
        row.update(actions=[Notification.read_at.set(now)])
        touched += 1
    return touched


def _trim(customer_sub, keep_rows=KEEP_ROWS):
    """Cap the feed length -- but never at the cost of something unseen.

    The TTL does the real expiry; this only stops a very busy account from growing an
    unbounded partition between expiries.

    **Unread rows are never deleted.** That was an explicit property of the Django
    version and it is the one that matters: losing something a customer has not seen is
    exactly the failure this feature exists to prevent. So the cap applies to read rows
    only, oldest first, and a feed of fifty unread entries simply stays fifty long.
    """
    rows = list(Notification.query(
        keys.customer_pk(customer_sub),
        range_key_condition=Notification.sk.startswith("NOTIF#"),
        scan_index_forward=False,
    ))
    if len(rows) <= keep_rows:
        return 0

    # Walk newest-first, keeping everything unread and the most recent read rows until
    # the budget runs out. Whatever is left over is read and old, and may go.
    budget, doomed = keep_rows, []
    for row in rows:
        if row.read_at is None:
            continue
        if budget > 0:
            budget -= 1
            continue
        doomed.append(row)

    if not doomed:
        return 0
    with Notification.batch_write() as batch:
        for row in doomed:
            batch.delete(row)
    return len(doomed)
