"""Slot generation and the rules that govern a booking.

Kept out of the views so every rule is enforced in one place, callable from a test, a
management command or an API handler. Nothing here trusts the caller: the UI hides slots
it should not offer, but hiding is not preventing.

The rules did not change when storage did. What changed is where they are enforced:
`select_for_update()` on the slot row became a set of `ConditionExpression`s inside one
`TransactWriteItems`, so the check and the write are the same operation and cannot go
stale between them. This module still owns every sentence a customer reads -- the store
raises typed errors and knows no wording at all, which is what lets every `BookingError`
string below survive the move unchanged.
"""

import datetime as dt

from django.utils import timezone

from . import identity
from . import mail
from .store import notifications as notification_store

from .choices import ACTIVE_STATUSES, BookingStatus, NotificationKind
from .booking_models import TestDriveSchedule
from .store import bookings as booking_store
from .store import customers as customer_store
from .store import slots as slot_store
from .store.errors import (
    AlreadyBooked,
    LimitReached,
    NotActive,
    NotFound,
    SlotUnavailable,
)

#: How far ahead customers may book.
HORIZON_DAYS = 28

#: No booking a slot that starts in five minutes - staff need warning, and it stops a
#: customer turning up before anyone has read the queue.
LEAD_MINUTES = 60

#: With no email verification, an account is free to create, so it must not be able to
#: swallow the whole calendar.
MAX_ACTIVE_BOOKINGS = 3


class BookingError(Exception):
    """Something the customer should be told about, in words they can act on."""


def ensure_slots(horizon_days=HORIZON_DAYS, now=None):
    """Materialise slots for every active rule across the horizon.

    Called when someone asks for availability rather than on a timer. Originally that
    was to avoid waking Aurora around the clock; the reason is gone but the design still
    earns its place, because it cannot drift out of step with the rules that generate it.

    Idempotent: the slot id is derived from (schedule, start time), so a conditional put
    either creates it or reports that it was already there. Concurrent requests race
    harmlessly, and a slot staff closed by hand is left exactly as they left it -- the
    condition refuses to touch an existing item at all.

    One read is reused rather than repeated: the slots already in the window are fetched
    once and only the missing ones are written. In the steady state that is two queries
    and zero writes, which matters because this runs on every availability request.
    """
    now = now or timezone.now()
    today = timezone.localdate(now)
    created = 0

    schedules = list(TestDriveSchedule.objects.filter(is_active=True))
    if not schedules:
        return 0

    horizon_end = now + dt.timedelta(days=horizon_days + 1)
    existing = slot_store.existing_ids(now - dt.timedelta(days=1), horizon_end)

    for offset in range(horizon_days + 1):
        day = today + dt.timedelta(days=offset)
        for schedule in schedules:
            if not schedule.covers(day):
                continue

            starts_at = _localise(day, schedule.start_time)
            ends_at = _localise(day, schedule.end_time)
            if ends_at <= starts_at:
                # An overnight window (22:00-01:00) ends the following day.
                ends_at += dt.timedelta(days=1)
            if ends_at <= now:
                continue

            schedule_id = str(schedule.pk)
            from .store import keys
            if keys.slot_id(schedule_id, starts_at) in existing:
                continue

            created += int(slot_store.ensure(
                schedule_id=schedule_id,
                starts_at=starts_at,
                ends_at=ends_at,
                capacity=schedule.capacity,
            ))

    return created


def _localise(day, time_of_day):
    """Combine a date and a wall-clock time in the shop's timezone."""
    naive = dt.datetime.combine(day, time_of_day)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def bookable_slots(now=None):
    """Slots a customer may actually choose, newest constraints applied.

    This is the list behind the picker, and the same window the booking path re-applies
    as a condition before writing.
    """
    now = now or timezone.now()
    earliest = now + dt.timedelta(minutes=LEAD_MINUTES)
    latest = now + dt.timedelta(days=HORIZON_DAYS)
    return [s for s in slot_store.between(earliest, latest) if s.is_open]


def active_bookings_for(user):
    return booking_store.for_customer(identity.sub_of(user), ACTIVE_STATUSES)


def _check_slot_is_offerable(slot, now):
    """Turn the slot as it actually stood into the sentence that explains the refusal.

    Called on the item DynamoDB hands back with the failed condition, so no second read
    is needed to work out which of these applies.
    """
    if not slot.is_open:
        raise BookingError("That time is no longer available.")

    if slot.starts_at <= now + dt.timedelta(minutes=LEAD_MINUTES):
        # Covers both "already in the past" and "too soon to arrange".
        raise BookingError(
            f"That time is too soon. Please choose a slot at least "
            f"{LEAD_MINUTES} minutes from now."
        )

    if slot.starts_at > now + dt.timedelta(days=HORIZON_DAYS):
        raise BookingError(
            f"Bookings open {HORIZON_DAYS} days ahead. Please choose an earlier date."
        )


def _explain(exc, now):
    """Map a typed store error onto the wording the customer sees."""
    if isinstance(exc, SlotUnavailable):
        if exc.slot is None:
            raise BookingError("That time is no longer available.") from exc
        _check_slot_is_offerable(exc.slot, now)
        # Every other reason ruled out, so the condition that failed was capacity.
        raise BookingError("That time has just been taken. Please choose another.") from exc
    if isinstance(exc, LimitReached):
        raise BookingError(
            f"You already have {MAX_ACTIVE_BOOKINGS} test drives booked. "
            "Cancel one before booking another."
        ) from exc
    if isinstance(exc, AlreadyBooked):
        raise BookingError("You have already booked that time.") from exc
    raise exc


def create_booking(*, user, slot_id, car=None, now=None):
    """Take a seat, or explain why not.

    No lock. The slot's capacity, the customer's limit and the one-live-booking guard
    are all conditions on a single transaction, so two people taking the last seat at
    the same moment cannot both succeed - the loser is told the time has just gone
    rather than turning up to find a stranger there.
    """
    now = now or timezone.now()
    sub = identity.sub_of(user)

    # The counter the limit is enforced against has to exist before it can be
    # incremented. Conditional, so two concurrent first bookings cannot reset it.
    customer_store.ensure(sub=sub, email=identity.email_of(user), now=now)

    slot = slot_store.find(str(slot_id))
    if slot is None:
        raise BookingError("That time is no longer available.")

    try:
        booking = booking_store.create(
            customer=_CustomerRef(user, sub), slot=slot, car=car, now=now,
            max_active=MAX_ACTIVE_BOOKINGS,
        )
    except Exception as exc:  # noqa: BLE001 - re-raised by _explain
        _explain(exc, now)

    # Queued, not sent: the write goes to S3 and a Lambda outside the VPC does the
    # sending. Failures are swallowed there - losing a notification must never cost the
    # customer their booking.
    mail.notify_staff_of_booking(booking, user)
    return booking


class _CustomerRef:
    """What the store needs to snapshot onto a booking and its seat.

    A narrow shim rather than passing the user straight through, so the store never
    learns what a Django user looks like - and so the same call works unchanged once
    Cognito supplies the identity.
    """

    def __init__(self, user, sub):
        self.sub = sub
        self.email = identity.email_of(user)
        self.phone = identity.phone_of(user)
        self._name = identity.full_name_of(user)

    def get_full_name(self):
        return self._name


def _bell_items(booking, kind, event, now):
    """The bell entry for a booking event, ready to go into the same transaction.

    Deliberately not a signal. A signal would also fire on paths that send no email -
    and a customer who sees "confirmed" in the app but never receives the message
    carrying the address, the licence reminder and the cancel link is worse off than one
    who saw nothing. Notifications belong next to the email, on the paths that send it.

    Returned rather than written, so the entry lands with the status change or not at
    all. Under Postgres that came from calling notify() inside the transaction; here it
    is one TransactWriteItems, which is a storage-layer guarantee rather than a
    session-scoped one.
    """
    if not identity.is_reachable(identity.user_for_sub(booking.customer_sub)):
        return []

    row, guard = notification_store.build(
        customer_sub=booking.customer_sub,
        kind=kind,
        context={
            "car_label": booking.car_label or "",
            "starts_at": booking.slot_starts_at.isoformat(),
            "booking_id": booking.booking_id,
        },
        dedupe_key=f"booking:{booking.booking_id}:{event}",
        now=now,
    )
    items = [("bell", row, None)]
    if guard is not None:
        items.append(("bell_guard", guard, type(guard).sk.does_not_exist()))
    return items


def confirm_booking(booking, now=None):
    """Staff accept a request. The only thing that emails the customer.

    Confirming twice is refused by the status condition rather than by an early return,
    so a double-submitted form cannot send a second email even if two requests arrive at
    once. That is stronger than the code it replaces.
    """
    now = now or timezone.now()
    if booking.status == BookingStatus.CONFIRMED:
        return booking

    try:
        booking = booking_store.set_status(
            booking, BookingStatus.CONFIRMED, now,
            extra=_bell_items(booking, NotificationKind.BOOKING_CONFIRMED,
                              "confirmed", now),
        )
    except NotActive:
        return booking

    mail.confirm_booking_with_customer(
        booking, identity.user_for_sub(booking.customer_sub))
    return booking


def cancel_by_staff(booking, now=None):
    """The shop calls it off, so the customer has to be told."""
    now = now or timezone.now()
    try:
        booking = booking_store.set_status(
            booking, BookingStatus.CANCELLED, now,
            extra=_bell_items(booking, NotificationKind.BOOKING_CANCELLED,
                              "cancelled", now),
        )
    except NotActive:
        return booking

    mail.notify_customer_of_cancellation(
        booking, identity.user_for_sub(booking.customer_sub))
    return booking


def cancel_booking(*, user, booking_id, now=None):
    now = now or timezone.now()
    booking = _own_active_booking(user, booking_id)
    try:
        return booking_store.set_status(booking, BookingStatus.CANCELLED, now)
    except NotActive:
        raise BookingError("That booking is no longer active.") from None


def reschedule_booking(*, user, booking_id, slot_id, now=None):
    """Move to another free slot, freeing the old one.

    The target goes through the same capacity condition a fresh booking does, so a move
    cannot overfill a slot that a new booking could not.
    """
    now = now or timezone.now()
    booking = _own_active_booking(user, booking_id)

    if str(booking.slot_id) == str(slot_id):
        # Load-bearing, not cosmetic: a transaction cannot contain two operations on one
        # item, so moving a booking to the slot it is already in would be rejected
        # outright by DynamoDB rather than being the no-op the customer expects.
        return booking

    slot = slot_store.find(str(slot_id))
    if slot is None:
        raise BookingError("That time is no longer available.")

    try:
        return booking_store.reschedule(booking=booking, slot=slot, now=now)
    except Exception as exc:  # noqa: BLE001 - re-raised by _explain
        _explain(exc, now)


def _own_active_booking(user, booking_id):
    """Ownership check.

    A booking id in a URL must never be enough to touch a stranger's appointment. That
    used to be a filter someone could forget; the customer is now part of the partition
    key, so a booking id alone simply does not address anything.
    """
    try:
        booking = booking_store.get(identity.sub_of(user), str(booking_id))
    except NotFound:
        raise BookingError("Booking not found.") from None

    if booking.status not in ACTIVE_STATUSES:
        raise BookingError("That booking is no longer active.")
    return booking
