"""Customers, read-only.

Replaces `CustomerProfileAdmin`, which was read-only for the same reason: customers
manage their own details, and staff only ever need to look one up while somebody is on
the phone.

The rows come from the store rather than from Cognito. Cognito holds the identity, but
`ListUsers` cannot filter by group, so listing customers through it would mean paging
the whole pool -- staff included -- and filtering client-side. The store's `CUSTOMER`
partition is exactly this list and nothing else.
"""

from django.shortcuts import render

from ..store import customers as store
from .auth import requires, staff_required


@staff_required
@requires("customer.view")
def customer_list(request):
    term = (request.GET.get("q") or "").strip().lower()

    people = store.all_customers()
    if term:
        # In Python rather than a FilterExpression: `contains` is case-sensitive and
        # cannot express the multi-field OR this replaces, and it would not reduce the
        # read cost anyway -- the partition is read either way.
        people = [c for c in people if term in (c.email or "").lower()]

    people.sort(key=lambda c: (c.email or "").lower())

    return render(request, "staff/customers/list.html", {
        "title": "Customers",
        "customers": people,
        "term": request.GET.get("q") or "",
    })
