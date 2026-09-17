"""The test drive queue, and the appointments behind it.

Replaces `TestDriveBookingAdmin` and `TestDriveSlotAdmin`.

Confirm and cancel route through `booking.confirm_booking` / `booking.cancel_by_staff`,
never through a direct write. That is what guarantees the customer is emailed and the
bell rings -- the old admin's `save_model` had to reach for the stored row to stop a
half-applied status sneaking past, and explicit action buttons remove the possibility
rather than working around it.
"""

import datetime as dt

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .. import booking as rules
from ..choices import ACTIVE_STATUSES, BookingStatus
from ..store import bookings as booking_store
from ..store import search
from ..store import slots as slot_store
from .auth import requires, staff_required
from .forms import BookingFilterForm, SlotFilterForm


def _find_booking(booking_id):
    """Look one up by its public id.

    A booking lives in its customer's partition, so the id alone does not address it --
    which is exactly the property that stops a customer reaching a stranger's
    appointment. Staff genuinely need the cross-customer view, and the queue is already
    one query, so it is scanned here rather than carrying a second index.
    """
    for booking in booking_store.staff_queue(
        [s for s in BookingStatus.values]
    ):
        if booking.booking_id == booking_id:
            return booking
    raise Http404("No such booking")


@staff_required
@requires("booking.view")
def booking_list(request):
    form = BookingFilterForm(request.GET or None)
    form.is_valid()
    data = form.cleaned_data if form.is_bound else {}

    chosen = data.get("status")
    statuses = [chosen] if chosen else list(ACTIVE_STATUSES)
    bookings = search.find_bookings(term=data.get("q") or None, statuses=statuses)

    return render(request, "staff/bookings/list.html", {
        "title": "Test drives",
        "form": form,
        "bookings": bookings,
        "pending": len(booking_store.staff_queue([BookingStatus.PENDING])),
    })


@staff_required
@requires("booking.view")
def booking_detail(request, booking_id):
    booking = _find_booking(booking_id)

    if request.method == "POST":
        return _handle(request, booking)

    return render(request, "staff/bookings/detail.html", {
        "title": "Test drive",
        "booking": booking,
        "roster": slot_store.roster(booking.slot_id),
    })


def _handle(request, booking):
    action = request.POST.get("action")
    here = reverse("staff:booking-detail", args=[booking.booking_id])

    if action == "confirm":
        return _confirm(request, booking, here)
    if action == "cancel":
        return _cancel(request, booking, here)
    if action in {"complete", "no_show"}:
        return _close_out(request, booking, here, action)

    messages.error(request, "Unknown action.")
    return redirect(here)


@requires("booking.change")
def _confirm(request, booking, here):
    """The only thing that tells a customer their appointment is on."""
    if booking.status == BookingStatus.CONFIRMED:
        messages.warning(request, "That booking is already confirmed.")
        return redirect(here)
    if not booking.is_active:
        messages.error(request, "That booking is no longer active.")
        return redirect(here)

    rules.confirm_booking(booking, now=timezone.now())
    messages.success(request, "Confirmed. The customer has been emailed.")
    return redirect(here)


@requires("booking.change")
def _cancel(request, booking, here):
    if not booking.is_active:
        messages.error(request, "That booking is no longer active.")
        return redirect(here)

    rules.cancel_by_staff(booking, now=timezone.now())
    messages.success(request, "Cancelled, and the customer has been told.")
    return redirect(here)


@requires("booking.change")
def _close_out(request, booking, here, action):
    """After the appointment: did they turn up or not.

    Neither emails anybody -- the visit has already happened. Both release the seat,
    which matters because the slot may still be in the future if staff are catching up.
    """
    if not booking.is_active:
        messages.error(request, "That booking is no longer active.")
        return redirect(here)

    status = (BookingStatus.COMPLETED if action == "complete"
              else BookingStatus.NO_SHOW)
    booking_store.set_status(booking, status, timezone.now())
    messages.success(
        request,
        "Marked as attended." if action == "complete" else "Marked as a no-show.",
    )
    return redirect(here)


# --------------------------------------------------------------------------------------
# Slots
# --------------------------------------------------------------------------------------

@staff_required
@requires("slot.view")
def slot_list(request):
    """Actual dates. This is where "that Friday is closed" gets done."""
    form = SlotFilterForm(request.GET or None)
    form.is_valid()
    data = form.cleaned_data if form.is_bound else {}

    now = timezone.now()
    # `date_hierarchy` became an explicit from/to pair, which is what staff actually
    # used it for and which maps straight onto a BETWEEN on the slot index.
    start = data.get("start") or timezone.localdate(now)
    end = data.get("end") or (timezone.localdate(now) + dt.timedelta(days=28))

    slots = slot_store.between(
        timezone.make_aware(dt.datetime.combine(start, dt.time.min)),
        timezone.make_aware(dt.datetime.combine(end, dt.time.max)),
    )
    if data.get("only_open"):
        slots = [s for s in slots if s.is_open]

    return render(request, "staff/slots/list.html", {
        "title": "Test drive slots",
        "form": form,
        "slots": slots,
        "start": start,
        "end": end,
    })


@staff_required
@requires("slot.change")
def slot_toggle(request, slot_id):
    """Close or re-open one appointment.

    Closing never cancels what is already booked. That was the old action's wording and
    it is still the right behaviour: staff close a slot to stop *new* bookings, and
    cancel the existing ones individually if the evening is genuinely off.
    """
    if request.method != "POST":
        raise Http404
    slot = slot_store.find(slot_id)
    if slot is None:
        raise Http404("No such slot")

    slot_store.set_open(slot_id, not slot.is_open)
    if slot.is_open:
        messages.success(
            request,
            "Closed. Anyone already booked still has their appointment - cancel those "
            "individually if the day is off.",
        )
    else:
        messages.success(request, "Re-opened.")
    return redirect(request.POST.get("next") or reverse("staff:slot-list"))
