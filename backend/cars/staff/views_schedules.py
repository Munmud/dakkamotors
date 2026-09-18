"""The recurring availability rules. Replaces `TestDriveScheduleAdmin`.

Staff describe availability as weekly rules; slots are generated from them whenever
someone asks for availability. That is why there is no "generate" button here and the
admin's action is not carried over -- it did nothing the next page view would not.
"""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from ..store import schedules as schedule_store
from ..store import slots as slot_store
from ..store.errors import ConditionFailed
from .auth import requires, staff_required
from .forms import ScheduleForm

import datetime as dt
from django.utils import timezone


@staff_required
@requires("schedule.view")
def schedule_list(request):
    return render(request, "staff/schedules/list.html", {
        "title": "Availability",
        "schedules": schedule_store.all_rules(),
        "form": ScheduleForm(),
    })


@staff_required
@requires("schedule.change")
def schedule_add(request):
    if request.method != "POST":
        return redirect(reverse("staff:schedule-list"))

    form = ScheduleForm(request.POST)
    if not form.is_valid():
        return render(request, "staff/schedules/list.html", {
            "title": "Availability",
            "schedules": schedule_store.all_rules(),
            "form": form,
        })

    try:
        schedule_store.create(**form.cleaned_data)
    except ConditionFailed as exc:
        messages.error(request, str(exc))
        return redirect(reverse("staff:schedule-list"))

    messages.success(
        request,
        "Rule added. Slots for it appear as soon as somebody asks for availability.",
    )
    return redirect(reverse("staff:schedule-list"))


@staff_required
@requires("schedule.change")
def schedule_edit(request, schedule_id):
    schedule = schedule_store.find(schedule_id)
    if schedule is None:
        raise Http404("No such rule")

    if request.method == "POST":
        if request.POST.get("action") == "delete":
            return _delete(request, schedule)
        return _save(request, schedule)

    return render(request, "staff/schedules/form.html", {
        "title": str(schedule),
        "schedule": schedule,
        "form": ScheduleForm(initial={
            "weekday": schedule.weekday,
            "start_time": schedule.start_time,
            "end_time": schedule.end_time,
            "capacity": schedule.capacity,
            "is_active": schedule.is_active,
            "starts_on": schedule.starts_on,
            "ends_on": schedule.ends_on,
            "note": schedule.note,
        }),
    })


def _save(request, schedule):
    form = ScheduleForm(request.POST)
    if not form.is_valid():
        return render(request, "staff/schedules/form.html", {
            "title": str(schedule), "schedule": schedule, "form": form,
        })
    try:
        schedule_store.update(schedule, **form.cleaned_data)
    except ConditionFailed as exc:
        messages.error(request, str(exc))
        return redirect(reverse("staff:schedule-edit", args=[schedule.schedule_id]))

    messages.success(
        request,
        "Saved. Slots already generated keep the capacity they were created with - "
        "editing a rule must not shrink an evening somebody has already booked.",
    )
    return redirect(reverse("staff:schedule-edit", args=[schedule.schedule_id]))


def _delete(request, schedule):
    """Remove the rule, and detach the future slots it produced.

    Slots people have already booked survive on purpose. That was `on_delete=SET_NULL`
    before; there is no referential action here, so the sweep is explicit -- and bounded,
    because only future slots matter.
    """
    now = timezone.now()
    detached = slot_store.clear_schedule(
        schedule.schedule_id, from_when=now, to_when=now + dt.timedelta(days=60))
    schedule_store.delete(schedule)

    messages.success(
        request,
        f"Rule deleted. {detached} future slot(s) kept, so nobody loses an appointment "
        "they already have - close them individually if the day is off.",
    )
    return redirect(reverse("staff:schedule-list"))
