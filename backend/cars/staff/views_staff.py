"""Staff accounts.

Replaces `StaffAccountAdmin`, and is deliberately stricter than it was. That page let an
inventory manager administer colleagues inside a sandbox -- a filtered queryset, a
narrowed fieldset, a `save_model` that forced `is_superuser` false, and a permission
hook that refused an owner target by direct URL. Four separate guards, because the
underlying page was powerful and access had to be clawed back.

Here `staff.*` is in `OWNER_ONLY`, so an inventory manager cannot reach these views at
all and none of that sandbox has to be rebuilt or re-proved. The escalation it defended
against is gone by construction rather than by four guards agreeing with each other.

What survives is the part that still applies to somebody who *does* have the power: an
owner can lock themselves out, and Cognito will help them do it.
"""

import secrets

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from .. import cognito
from .auth import requires, staff_required
from .forms import GROUP_LABELS, StaffAccountForm

#: Long enough that it is never guessed, short enough to read down a phone line.
TEMPORARY_PASSWORD_LENGTH = 16


def _temporary_password():
    """A first password, shown once.

    The pool sends no email, so there is no "you have been invited" message to carry a
    link -- the owner passes this on themselves. Cognito requires it be changed at first
    sign-in, so its lifetime is one use.
    """
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet)
                   for _ in range(TEMPORARY_PASSWORD_LENGTH)) + "!7"


def _roster():
    """Everyone who can sign in to these pages, owners last.

    Built from the `staff` group rather than from the whole pool: the pool also holds
    every customer, and `ListUsers` cannot filter by group.
    """
    owners = {u["Username"] for u in cognito.users_in_group(cognito.OWNERS_GROUP)}
    people = []
    for raw in cognito.users_in_group(cognito.STAFF_GROUP):
        attrs = {a["Name"]: a["Value"] for a in raw.get("Attributes", [])}
        username = raw["Username"]
        groups = cognito.groups_of(username)
        people.append({
            "username": username,
            "email": attrs.get("email", ""),
            "name": " ".join(filter(None, [attrs.get("given_name", ""),
                                           attrs.get("family_name", "")])).strip(),
            "groups": groups,
            "labels": [GROUP_LABELS.get(g, g) for g in groups],
            "is_owner": username in owners,
            "is_active": raw.get("Enabled", True),
            "status": raw.get("UserStatus", ""),
        })
    people.sort(key=lambda p: (not p["is_owner"], p["email"].lower()))
    return people


def _find(username):
    for person in _roster():
        if person["username"] == username:
            return person
    raise Http404("No such staff account")


@staff_required
@requires("staff.view")
def staff_list(request):
    return render(request, "staff/accounts/list.html", {
        "title": "Staff",
        "people": _roster(),
        "me": request.staff.sub,
    })


@staff_required
@requires("staff.add")
def staff_add(request):
    form = StaffAccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        password = _temporary_password()
        try:
            cognito.create_staff(
                email=form.cleaned_data["email"],
                name=form.cleaned_data["name"],
                temporary_password=password,
            )
        except cognito.CognitoError as exc:
            form.add_error(None, str(exc))
        else:
            for group in form.cleaned_data["groups"]:
                cognito.add_to_group(form.cleaned_data["email"], group)
            if not form.cleaned_data["is_active"]:
                cognito.set_enabled(form.cleaned_data["email"], False)

            # Shown once, in the response to the request that created it. Nothing stores
            # it and no email carries it, so if this message is missed the owner resets
            # the account rather than looks it up.
            messages.success(
                request,
                f"Created {form.cleaned_data['email']}. Their first password is "
                f"{password} — pass it on now, it is not shown again and they will be "
                f"asked to change it when they sign in.",
            )
            return redirect(reverse("staff:staff-list"))

    return render(request, "staff/accounts/form.html", {
        "title": "Add a staff account",
        "form": form,
        "person": None,
    })


@staff_required
@requires("staff.change")
def staff_edit(request, username):
    person = _find(username)
    editing_self = person["username"] == request.staff.sub

    if request.method == "POST":
        form = StaffAccountForm(
            request.POST, editing_self=editing_self, existing=True,
            initial={"email": person["email"]},
        )
        if form.is_valid():
            wanted = set(form.cleaned_data["groups"])
            # Only the assignable groups are touched. An owner editing another owner
            # must not strip `owners` as a side effect of a form that never offered it.
            for group in form.cleaned_data["groups"]:
                if group not in person["groups"]:
                    cognito.add_to_group(person["username"], group)
            for group in person["groups"]:
                if group in dict(form.fields["groups"].choices) and group not in wanted:
                    cognito.remove_from_group(person["username"], group)

            if form.cleaned_data["is_active"] != person["is_active"]:
                cognito.set_enabled(person["username"],
                                    form.cleaned_data["is_active"])

            first, _, last = (form.cleaned_data["name"] or "").partition(" ")
            cognito.update_attributes(person["username"],
                                      first_name=first, last_name=last)
            messages.success(request, f"Saved {person['email']}.")
            return redirect(reverse("staff:staff-list"))
    else:
        form = StaffAccountForm(
            editing_self=editing_self, existing=True,
            initial={
                "email": person["email"],
                "name": person["name"],
                "groups": person["groups"],
                "is_active": person["is_active"],
            },
        )

    return render(request, "staff/accounts/form.html", {
        "title": person["email"],
        "form": form,
        "person": person,
        "editing_self": editing_self,
    })
