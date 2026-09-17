"""Booking storage: the replacement for `select_for_update()`.

The old design locked the slot row and counted inside the lock, because without it two
people taking the last seat both read "1 left" and both succeeded -- and the dealer found
out when two strangers arrived for one car.

There is no row lock here. Instead every rule is a `ConditionExpression` inside a single
`TransactWriteItems`, so the check and the write are the same operation and cannot go
stale between them:

    slot      booked_count < capacity, is_open, and inside the booking window
    customer  active_bookings < MAX_ACTIVE_BOOKINGS
    seat      attribute_not_exists  -- one live booking per customer per slot
    booking   attribute_not_exists

On the happy path that is **one round trip and no reads at all**. On failure DynamoDB
hands back the offending slot (`return_values=ALL_OLD`), so the caller can explain
precisely what went wrong without a second read.

This module raises typed errors and never wording. `booking.py` owns every sentence a
customer sees, which is what lets its `BookingError` strings -- asserted verbatim all
over the test suite -- survive the migration unchanged.
"""

import datetime as dt

from ..choices import ACTIVE_STATUSES, BookingStatus
from . import keys
from .errors import (
    AlreadyBooked,
    LimitReached,
    NotActive,
    NotFound,
    SlotUnavailable,
)
from .models import Booking, Customer, Seat, Slot
from .txn import Txn, failed, reason_for

#: Transaction labels. Never index `cancellation_reasons` by number -- see store/txn.py.
SLOT, CUSTOMER, SEAT, BOOKING = "slot", "customer", "seat", "booking"
OLD_SLOT, OLD_SEAT = "old_slot", "old_seat"


def _snapshot(customer):
    """Denormalise the customer onto the booking and the seat.

    Staff need name, email and phone on the booking queue. Reading them from Cognito per
    row would be an N+1 against a remote API; the same instinct already produced
    `car_label`, so this follows it. The snapshot is refreshed across a customer's (at
    most three) active bookings when they edit their profile.
    """
    return {
        "customer_name": (customer.get_full_name() or "").strip(),
        "customer_email": customer.email or "",
        "customer_phone": getattr(customer, "phone", "") or "",
    }


def _car_id(car):
    """The car's store identifier, from either representation.

    Mirrors `cars.identity.car_id_of`, kept local so the store package does not import
    from the app it serves.
    """
    if car is None:
        return None
    return getattr(car, "car_id", None) or str(getattr(car, "pk", "")) or None


def create(*, customer, slot, car, now, max_active):
    """Take a seat, or raise something the domain layer can turn into a sentence."""
    booking_id = keys.new_id()
    sub = customer.sub
    snap = _snapshot(customer)
    car_label = car.seo_title_plain if car else ""

    booking = Booking(
        pk=keys.customer_pk(sub),
        sk=keys.booking_sk(booking_id),
        booking_id=booking_id,
        customer_sub=sub,
        slot_id=slot.slot_id,
        slot_starts_at=slot.starts_at,
        slot_ends_at=slot.ends_at,
        car_id=_car_id(car),
        car_label=car_label,
        car_slug=getattr(car, "slug", None),
        status=BookingStatus.PENDING,
        created_at=now,
        updated_at=now,
        gsi1pk=keys.booking_status_gsi1pk(BookingStatus.PENDING),
        gsi1sk=keys.booking_gsi1sk(slot.starts_at, slot.slot_id, booking_id),
        **snap,
    )
    seat = Seat(
        pk=keys.slot_pk(slot.slot_id),
        sk=keys.seat_sk(sub),
        booking_id=booking_id,
        customer_sub=sub,
        car_label=car_label,
        created_at=now,
        **snap,
    )

    tx = Txn()
    tx.update(
        SLOT,
        Slot(pk=keys.slot_pk(slot.slot_id), sk=keys.META),
        actions=[Slot.booked_count.add(1)],
        condition=_offerable(now),
    )
    tx.update(
        CUSTOMER,
        Customer(pk=keys.customer_pk(sub), sk=keys.PROFILE),
        actions=[Customer.active_bookings.add(1)],
        condition=(Customer.pk.exists() & (Customer.active_bookings < max_active)),
    )
    tx.save(SEAT, seat, condition=Seat.sk.does_not_exist())
    tx.save(BOOKING, booking, condition=Booking.sk.does_not_exist())

    _commit(tx, on_slot_failure=SLOT)
    return booking


def _offerable(now, *, lead_minutes=60, horizon_days=28):
    """The slot half of the booking rules, as a condition rather than a read.

    `booked_count < capacity` compares two document paths, which is what makes the
    capacity check atomic without a lock. The time bounds are compared as strings, which
    is sound only because timestamps are fixed-width UTC -- see store/keys.iso.
    """
    earliest = now + dt.timedelta(minutes=lead_minutes)
    latest = now + dt.timedelta(days=horizon_days)
    return (
        Slot.pk.exists()
        & (Slot.is_open == True)  # noqa: E712 -- PynamoDB needs the comparison, not `is`
        & (Slot.booked_count < Slot.capacity)
        & (Slot.starts_at > earliest)
        & (Slot.starts_at <= latest)
    )


def _commit(tx, *, on_slot_failure):
    """Translate a cancelled transaction into a typed error, by label.

    The check order deliberately mirrors the old `create_booking`: offerability first,
    then the per-customer limit, then the duplicate, then capacity. Reproducing that
    order is what keeps the customer-facing messages identical.
    """
    order = tx.labels_in_wire_order()
    try:
        tx.commit()
    except Exception as exc:
        reasons = getattr(exc, "reasons", None)
        if reasons is None:
            raise

        if failed(exc, on_slot_failure, order):
            reason = reason_for(exc, on_slot_failure, order)
            raw = reason.raw_item if reason else None
            # No item means the slot is simply not there. Otherwise hand the slot as it
            # actually stood to the domain layer, which decides between "closed", "too
            # soon", "too far ahead" and -- once those are ruled out -- "just taken".
            raise SlotUnavailable(Slot.from_raw_data(raw) if raw else None) from exc

        if CUSTOMER in order and failed(exc, CUSTOMER, order):
            raise LimitReached() from exc
        if SEAT in order and failed(exc, SEAT, order):
            raise AlreadyBooked() from exc
        raise


def get(customer_sub, booking_id):
    try:
        return Booking.get(keys.customer_pk(customer_sub), keys.booking_sk(booking_id))
    except Booking.DoesNotExist:
        raise NotFound(f"no booking {booking_id!r}") from None


def for_customer(customer_sub, statuses=ACTIVE_STATUSES):
    """A customer's bookings, from their own partition. No index needed."""
    rows = Booking.query(
        keys.customer_pk(customer_sub),
        range_key_condition=Booking.sk.startswith("BOOKING#"),
    )
    if statuses is None:
        return list(rows)
    wanted = set(statuses)
    return [b for b in rows if b.status in wanted]


def staff_queue(statuses=ACTIVE_STATUSES):
    """The booking queue, soonest appointment first.

    GSI1 sorts by slot start time, which is what `Meta.ordering = ["slot__starts_at"]`
    used to do through a join.
    """
    out = []
    for status in statuses:
        out.extend(Booking.gsi1.query(keys.booking_status_gsi1pk(status)))
    out.sort(key=lambda b: (b.slot_starts_at or dt.datetime.max))
    return out


def set_status(booking, status, now, extra=None):
    """Move a booking to a terminal or confirmed state, releasing the seat if needed.

    The status condition is the idempotency gate for the whole operation. Confirming
    twice therefore cannot double-decrement or send a second email -- that is now a
    property of the write rather than an early `return`, which is stronger than the code
    it replaces.
    """
    leaving_active = booking.is_active and status not in ACTIVE_STATUSES
    stamp = {
        BookingStatus.CONFIRMED: Booking.confirmed_at,
        BookingStatus.CANCELLED: Booking.cancelled_at,
    }.get(status)

    actions = [
        Booking.status.set(status),
        Booking.updated_at.set(now),
        Booking.gsi1pk.set(keys.booking_status_gsi1pk(status)),
    ]
    if stamp is not None:
        actions.append(stamp.set(now))

    # Confirming requires PENDING rather than merely "active". Two reasons: it makes
    # "confirming twice" a refusal at the storage layer rather than an early return an
    # in-memory copy could be stale about, and it stops a second confirmation colliding
    # with the notification guard below and failing in a way that reads like a bug.
    allowed = ([BookingStatus.PENDING] if status == BookingStatus.CONFIRMED
               else list(ACTIVE_STATUSES))

    tx = Txn()
    tx.update(
        BOOKING,
        Booking(pk=booking.pk, sk=booking.sk),
        actions=actions,
        condition=Booking.status.is_in(*allowed),
    )
    # Anything the caller wants to land with the status change -- in practice the bell
    # entry, so a confirmation that does not happen leaves no notification behind.
    for label, item, condition in (extra or []):
        tx.save(label, item, condition=condition)
    if leaving_active:
        tx.update(
            SLOT,
            Slot(pk=keys.slot_pk(booking.slot_id), sk=keys.META),
            actions=[Slot.booked_count.add(-1)],
            # A drift detector, not a correctness guarantee -- the status condition
            # above is what makes this run at most once.
            condition=Slot.booked_count > 0,
        )
        tx.update(
            CUSTOMER,
            Customer(pk=keys.customer_pk(booking.customer_sub), sk=keys.PROFILE),
            actions=[Customer.active_bookings.add(-1)],
            condition=Customer.active_bookings > 0,
        )
        # Unconditional: a cancel must never wedge because the guard is already gone.
        tx.delete(SEAT, Seat(pk=keys.slot_pk(booking.slot_id),
                             sk=keys.seat_sk(booking.customer_sub)))

    order = tx.labels_in_wire_order()
    try:
        tx.commit()
    except Exception as exc:
        if getattr(exc, "reasons", None) is not None and failed(exc, BOOKING, order):
            raise NotActive() from exc
        raise

    booking.status = status
    booking.updated_at = now
    if stamp is not None:
        setattr(booking, stamp.attr_name or "confirmed_at", now)
    return booking


def reschedule(*, booking, slot, now):
    """Move to another free slot, freeing the old one, in one transaction.

    The target goes through the same capacity condition a fresh booking does, so a move
    cannot overfill a slot that a new booking could not.

    The caller MUST return early when the target is the current slot. DynamoDB rejects a
    transaction containing two operations on one item, so that early return is
    load-bearing rather than cosmetic.
    """
    if booking.slot_id == slot.slot_id:
        raise ValueError("reschedule to the same slot must be short-circuited by the "
                         "caller: a transaction cannot touch one item twice")

    sub = booking.customer_sub
    tx = Txn()
    tx.update(
        SLOT,
        Slot(pk=keys.slot_pk(slot.slot_id), sk=keys.META),
        actions=[Slot.booked_count.add(1)],
        condition=_offerable(now),
    )
    tx.update(
        OLD_SLOT,
        Slot(pk=keys.slot_pk(booking.slot_id), sk=keys.META),
        actions=[Slot.booked_count.add(-1)],
        condition=Slot.booked_count > 0,
    )
    tx.delete(OLD_SEAT, Seat(pk=keys.slot_pk(booking.slot_id), sk=keys.seat_sk(sub)))
    tx.save(
        SEAT,
        Seat(pk=keys.slot_pk(slot.slot_id), sk=keys.seat_sk(sub),
             booking_id=booking.booking_id, customer_sub=sub,
             car_label=booking.car_label, created_at=now,
             customer_name=booking.customer_name,
             customer_email=booking.customer_email,
             customer_phone=booking.customer_phone),
        condition=Seat.sk.does_not_exist(),
    )
    tx.update(
        BOOKING,
        Booking(pk=booking.pk, sk=booking.sk),
        actions=[
            Booking.slot_id.set(slot.slot_id),
            Booking.slot_starts_at.set(slot.starts_at),
            Booking.slot_ends_at.set(slot.ends_at),
            Booking.updated_at.set(now),
            Booking.gsi1sk.set(keys.booking_gsi1sk(
                slot.starts_at, slot.slot_id, booking.booking_id)),
        ],
        condition=Booking.status.is_in(*ACTIVE_STATUSES),
    )

    _commit(tx, on_slot_failure=SLOT)

    booking.slot_id = slot.slot_id
    booking.slot_starts_at = slot.starts_at
    booking.slot_ends_at = slot.ends_at
    booking.updated_at = now
    return booking


def refresh_customer_snapshot(customer):
    """Re-stamp name/email/phone across a customer's active bookings and seats.

    Bounded by MAX_ACTIVE_BOOKINGS, so this is at most three bookings and three seats --
    which is the whole reason the snapshot is affordable.
    """
    snap = _snapshot(customer)
    touched = 0
    for booking in for_customer(customer.sub):
        booking.update(actions=[
            Booking.customer_name.set(snap["customer_name"]),
            Booking.customer_email.set(snap["customer_email"]),
            Booking.customer_phone.set(snap["customer_phone"]),
        ])
        try:
            seat = Seat.get(keys.slot_pk(booking.slot_id),
                            keys.seat_sk(customer.sub))
        except Seat.DoesNotExist:
            continue
        seat.update(actions=[
            Seat.customer_name.set(snap["customer_name"]),
            Seat.customer_email.set(snap["customer_email"]),
            Seat.customer_phone.set(snap["customer_phone"]),
        ])
        touched += 1
    return touched
