"""Slot storage.

The interesting one is `ensure`. Slots are materialised lazily when someone asks for
availability rather than on a timer -- originally because a scheduled job would have kept
Aurora awake around the clock and undone the scale-to-zero saving. That reason is gone
with Aurora, but the design survives on its own merit: it is one fewer moving part, and
it cannot drift out of step with the rules that generate it.
"""

import datetime as dt

from pynamodb.exceptions import PutError

from . import keys
from .errors import NotFound, is_conditional_failure
from .models import Seat, Slot


def ensure(*, schedule_id, starts_at, ends_at, capacity):
    """Create one slot if it is not already there. Returns True when it created it.

    This is `get_or_create` with **no read and no race**: `slot_id` is derived from
    (schedule, start time), so the primary key *is* the old
    `unique_slot_per_rule_occurrence` constraint, and a conditional put either wins or
    tells us it was already there.

    It also preserves a behaviour the old code had to be careful about: because the
    condition refuses to touch an existing item, a slot staff closed by hand is left
    exactly as they left it. Regenerating never re-opens a closed evening.
    """
    sid = keys.slot_id(schedule_id, starts_at)
    slot = Slot(
        pk=keys.slot_pk(sid),
        sk=keys.META,
        slot_id=sid,
        schedule_id=schedule_id,
        starts_at=starts_at,
        ends_at=ends_at,
        capacity=capacity,
        is_open=True,
        # Must be initialised: `if_not_exists()` is legal only in an UpdateExpression,
        # never in a ConditionExpression, so `booked_count < capacity` needs a number
        # to compare against from the very first booking.
        booked_count=0,
        gsi1pk=keys.slot_month_gsi1pk(starts_at),
        gsi1sk=keys.slot_gsi1sk(starts_at, sid),
    )
    try:
        slot.save(condition=Slot.pk.does_not_exist())
        return True
    except PutError as exc:
        if is_conditional_failure(exc):
            return False
        raise





def get(slot_id):
    try:
        return Slot.get(keys.slot_pk(slot_id), keys.META)
    except Slot.DoesNotExist:
        raise NotFound(f"no slot {slot_id!r}") from None


def find(slot_id):
    """Like `get`, but returns None instead of raising."""
    try:
        return get(slot_id)
    except NotFound:
        return None


def between(earliest, latest):
    """Open slots starting inside a window, soonest first.

    GSI1 shards slots by calendar month, so a 28-day horizon touches one or two
    partitions. Querying each and merging keeps the partition from becoming a single
    hot key without costing more than one extra request.
    """
    out = []
    for month in _months_spanned(earliest, latest):
        out.extend(Slot.gsi1.query(
            month,
            range_key_condition=Slot.gsi1sk.between(
                keys.slot_gsi1sk(earliest, ""), keys.slot_gsi1sk(latest, "￿")
            ),
        ))
    out.sort(key=lambda s: s.starts_at)
    return out


def _months_spanned(start, end):
    seen, cursor = [], start.replace(day=1)
    while cursor <= end:
        key = keys.slot_month_gsi1pk(cursor)
        if key not in seen:
            seen.append(key)
        # Step to the first of the next month without needing calendar arithmetic.
        cursor = (cursor.replace(day=28) + dt.timedelta(days=7)).replace(day=1)
    return seen


def existing_ids(earliest, latest):
    """The slot ids already materialised in a window.

    `ensure_slots` runs on every availability request. The naive port would issue one
    conditional put per rule per day -- 28 x N writes on a page view. Instead the caller
    reuses the query it was going to make anyway and writes only what is missing, so the
    steady state is two queries and zero writes.
    """
    return {slot.slot_id for slot in between(earliest, latest)}


def roster(slot_id):
    """Who is actually coming, from the LIVE# seat items.

    No index and no join: the seats live in the slot's own partition and carry a
    snapshot of the customer, including the phone number staff need. This is also the
    source of truth that `reconcile_counters` checks `booked_count` against.
    """
    return list(Seat.query(
        keys.slot_pk(slot_id),
        range_key_condition=Seat.sk.startswith("LIVE#"),
    ))


def set_open(slot_id, is_open):
    slot = get(slot_id)
    slot.update(actions=[Slot.is_open.set(bool(is_open))])
    return slot


def set_capacity(slot_id, capacity):
    """Staff widening or narrowing one awkward date, without touching the rule."""
    slot = get(slot_id)
    slot.update(actions=[Slot.capacity.set(int(capacity))])
    return slot


def clear_schedule(schedule_id, *, from_when, to_when):
    """Detach future slots from a rule that is being deleted.

    The old model used `on_delete=SET_NULL` so that slots people had already booked
    survived the rule's removal. There is no referential action here, so the sweep is
    explicit -- and bounded, because only future slots matter.
    """
    cleared = 0
    for slot in between(from_when, to_when):
        if slot.schedule_id == schedule_id:
            slot.update(actions=[Slot.schedule_id.remove()])
            cleared += 1
    return cleared
