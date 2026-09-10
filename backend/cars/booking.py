"""Slot generation and the rules that govern a booking.

Kept out of the views so every rule is enforced in one place, callable from a test, a
management command or an API handler. Nothing here trusts the caller: the UI hides slots
it should not offer, but hiding is not preventing.
"""

import datetime as dt

from django.db import transaction
from django.utils import timezone

from .booking_models import (
    BookingStatus,
    TestDriveBooking,
    TestDriveSchedule,
    TestDriveSlot,
)

#: How far ahead customers may book.
HORIZON_DAYS = 28

#: No booking a slot that starts in five minutes - staff need warning, and it stops a
#: customer turning up before anyone has read the admin.
LEAD_MINUTES = 60

#: With no email verification, an account is free to create, so it must not be able to
#: swallow the whole calendar.
MAX_ACTIVE_BOOKINGS = 3


class BookingError(Exception):
    """Something the customer should be told about, in words they can act on."""


def ensure_slots(horizon_days=HORIZON_DAYS, now=None):
    """Materialise slots for every active rule across the horizon.

    Called when someone asks for availability rather than on a timer. A scheduled job
    would query the database every few minutes and keep Aurora awake permanently, which
    would undo the scale-to-zero saving; by the time this runs, the request has already
    woken it.

    Idempotent: `get_or_create` on (schedule, starts_at), so concurrent requests race
    harmlessly and a slot staff closed by hand is left exactly as they left it.
    """
    now = now or timezone.now()
    today = timezone.localdate(now)
    created = 0

    schedules = list(TestDriveSchedule.objects.filter(is_active=True))
    if not schedules:
        return 0

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

            _, made = TestDriveSlot.objects.get_or_create(
                schedule=schedule,
                starts_at=starts_at,
                defaults={"ends_at": ends_at, "capacity": schedule.capacity},
            )
            created += int(made)

    return created


def _localise(day, time_of_day):
    """Combine a date and a wall-clock time in the shop's timezone."""
    naive = dt.datetime.combine(day, time_of_day)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def bookable_slots(now=None):
    """Slots a customer may actually choose, newest constraints applied.

    This is the queryset behind the picker, and the same filter the booking path
    re-applies before writing.
    """
    now = now or timezone.now()
    earliest = now + dt.timedelta(minutes=LEAD_MINUTES)
    latest = now + dt.timedelta(days=HORIZON_DAYS)
    return (
        TestDriveSlot.objects.filter(
            is_open=True, starts_at__gte=earliest, starts_at__lte=latest
        )
        .select_related("schedule")
        .order_by("starts_at")
    )


def active_bookings_for(user):
    return TestDriveBooking.objects.filter(
        customer=user, status=BookingStatus.BOOKED
    ).select_related("slot", "car")


def _check_slot_is_offerable(slot, now):
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


@transaction.atomic
def create_booking(*, user, slot_id, car=None, now=None):
    """Take a seat, or explain why not.

    The slot row is locked for the duration. Without that, two people taking the last
    seat at the same moment both read "1 left" and both succeed - and the dealer finds
    out when two strangers arrive for one car.
    """
    now = now or timezone.now()

    try:
        slot = TestDriveSlot.objects.select_for_update().get(pk=slot_id)
    except TestDriveSlot.DoesNotExist:
        raise BookingError("That time is no longer available.") from None

    _check_slot_is_offerable(slot, now)

    if active_bookings_for(user).count() >= MAX_ACTIVE_BOOKINGS:
        raise BookingError(
            f"You already have {MAX_ACTIVE_BOOKINGS} test drives booked. "
            "Cancel one before booking another."
        )

    if TestDriveBooking.objects.filter(
        slot=slot, customer=user, status=BookingStatus.BOOKED
    ).exists():
        raise BookingError("You have already booked that time.")

    # Counted inside the lock, so it cannot go stale between check and write.
    taken = TestDriveBooking.objects.filter(
        slot=slot, status=BookingStatus.BOOKED
    ).count()
    if taken >= slot.capacity:
        raise BookingError("That time has just been taken. Please choose another.")

    return TestDriveBooking.objects.create(
        slot=slot,
        customer=user,
        car=car,
        car_label=car.seo_title_plain if car else "",
    )


@transaction.atomic
def cancel_booking(*, user, booking_id, now=None):
    now = now or timezone.now()
    booking = _own_active_booking(user, booking_id)

    booking.status = BookingStatus.CANCELLED
    booking.cancelled_at = now
    booking.save(update_fields=["status", "cancelled_at", "updated_at"])
    return booking


@transaction.atomic
def reschedule_booking(*, user, booking_id, slot_id, now=None):
    """Move to another free slot, freeing the old one.

    Done as a cancel-and-rebook against a locked target so the capacity check is the
    same one a fresh booking goes through - a move must not be able to overfill a slot
    that a normal booking could not.
    """
    now = now or timezone.now()
    booking = _own_active_booking(user, booking_id)

    if str(booking.slot_id) == str(slot_id):
        return booking

    try:
        slot = TestDriveSlot.objects.select_for_update().get(pk=slot_id)
    except TestDriveSlot.DoesNotExist:
        raise BookingError("That time is no longer available.") from None

    _check_slot_is_offerable(slot, now)

    taken = (
        TestDriveBooking.objects.filter(slot=slot, status=BookingStatus.BOOKED)
        .exclude(pk=booking.pk)
        .count()
    )
    if taken >= slot.capacity:
        raise BookingError("That time has just been taken. Please choose another.")

    if (
        TestDriveBooking.objects.filter(
            slot=slot, customer=user, status=BookingStatus.BOOKED
        )
        .exclude(pk=booking.pk)
        .exists()
    ):
        raise BookingError("You have already booked that time.")

    booking.slot = slot
    booking.save(update_fields=["slot", "updated_at"])
    return booking


def _own_active_booking(user, booking_id):
    """Ownership check.

    A booking id in a URL must never be enough to touch a stranger's appointment, so
    the customer is part of the lookup rather than something verified afterwards.
    """
    try:
        booking = TestDriveBooking.objects.select_related("slot").get(
            pk=booking_id, customer=user
        )
    except TestDriveBooking.DoesNotExist:
        raise BookingError("Booking not found.") from None

    if booking.status != BookingStatus.BOOKED:
        raise BookingError("That booking is no longer active.")
    return booking
