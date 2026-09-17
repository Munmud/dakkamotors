"""Who may reach the staff pages.

One decorator, in one place, so that swapping Django auth for Cognito is a change to
this file and nothing else. Today it is `staff_member_required`, which is exactly what
`/api/admin/` already enforces -- the staff pages are no more or less privileged than
the admin they replace.

When Cognito lands this becomes: read the `dm_st` cookie, verify the JWT, require the
`staff` group, and require the token's `client_id` to be the staff app client (a
customer-client token carrying a staff group must not be enough -- that one string
comparison is the whole staff/customer isolation).
"""

from functools import wraps

from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied


def staff_required(view):
    """Signed in, active, and staff.

    Redirects to the admin login rather than 403ing, so a staff member who followed a
    link from an email lands somewhere useful.
    """
    return staff_member_required(view, login_url="/api/admin/login/")


def requires(action):
    """Guard one action behind a permission.

    Written as an explicit action string rather than derived from the model, matching
    `ensure_inventory_group`'s discipline: what a role may do should be readable in one
    list, not reconstructed from an app label.

    While Django auth is still in place this maps onto the existing permissions, so an
    Inventory Manager keeps exactly the access they have today.
    """
    permission = _PERMISSION_FOR.get(action)

    def decorate(view):
        @wraps(view)
        def guarded(request, *args, **kwargs):
            if permission and not request.user.has_perm(permission):
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return guarded
    return decorate


#: Action -> Django permission, for as long as Django holds the permissions. The Cognito
#: replacement is a group -> set-of-actions map; keeping the action names stable now
#: means that swap touches only this table.
_PERMISSION_FOR = {
    "question.view": "cars.view_carquestion",
    "question.change": "cars.change_carquestion",
    "question.delete": "cars.delete_carquestion",
    "booking.view": "cars.view_testdrivebooking",
    "booking.change": "cars.change_testdrivebooking",
    "slot.view": "cars.view_testdriveslot",
    "slot.change": "cars.change_testdriveslot",
    "car.view": "cars.view_car",
    "car.add": "cars.add_car",
    "car.change": "cars.change_car",
    "car.delete": "cars.delete_car",
}
