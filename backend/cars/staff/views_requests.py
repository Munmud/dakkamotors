"""The list of cars people are looking for.

A lead queue rather than a changelist: open ones first, and one button per request
that says what it does. Every write goes through `requests.*`, the domain module, for
the same reason the question pages go through `qa.*` -- reaching the item another way
must not skip a rule.
"""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from .. import requests as domain
from ..choices import RequestStatus
from ..store import requests as store
from ..store.errors import NotFound
from .auth import requires, staff_required
from .forms import RequestFilterForm, ResolveRequestForm


def _find(request_id):
    try:
        return store.get(request_id)
    except NotFound:
        raise Http404("No such request")


@staff_required
@requires("request.view")
def request_list(request):
    form = RequestFilterForm(request.GET or None)
    form.is_valid()
    data = form.cleaned_data if form.is_bound else {}
    # Open by default. The list is for acting on; the resolved ones are a record.
    state = data.get("state") if form.is_bound else RequestStatus.OPEN

    rows = store.queue()
    if state in (RequestStatus.OPEN, RequestStatus.RESOLVED):
        rows = [r for r in rows if r.status == state]

    return render(request, "staff/requests/list.html", {
        "title": "Car requests",
        "form": form if form.is_bound else RequestFilterForm(initial={"state": state}),
        "requests": rows,
        "state": state,
        "open_count": store.open_count(),
    })


@staff_required
@requires("request.view")
def request_detail(request, request_id):
    record = _find(request_id)

    if request.method == "POST":
        return _handle_action(request, record)

    return render(request, "staff/requests/detail.html", {
        "title": "Car request",
        "req": record,
        "form": ResolveRequestForm(initial={"note": record.resolution_note or ""}),
    })


def _handle_action(request, record):
    action = request.POST.get("action")
    here = reverse("staff:request-detail", args=[record.request_id])

    if action == "resolve":
        return _resolve(request, record, here)
    if action == "reopen":
        return _reopen(request, record, here)

    messages.error(request, "That button did not do anything. Try again, and if it "
                            "keeps happening reload the page.")
    return redirect(here)


@requires("request.change")
def _resolve(request, record, here):
    form = ResolveRequestForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Could not save that note.")
        return redirect(here)
    domain.resolve(record, staff=request.staff, note=form.cleaned_data["note"])
    messages.success(request, f"Resolved. {record.name} is off the open list.")
    return redirect(reverse("staff:request-list"))


@requires("request.change")
def _reopen(request, record, here):
    domain.reopen(record)
    messages.success(request, "Reopened.")
    return redirect(here)
