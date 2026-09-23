"""What the masthead of every staff page needs to know.

Two questions, one per processor: what is waiting, and what this person may open.

A template context processor rather than something each view passes: the number
belongs on the tab, and a tab is on every page. It returns nothing unless the request
has a signed-in staff member on it -- the public pages render through f-strings in
`pages.py` and never reach the template engine, so they never pay for this.

Three Queries per staff page, for two people, uncached on purpose. A count that lags
is a count that lies, and the whole point of putting it on the tab is that it is right
when you look up. Each number is the same definition the section's own page uses, so
the tab and the page can never disagree.
"""

from ..choices import BookingStatus
from ..store import bookings as booking_store
from ..store import questions as question_store
from ..store import requests as request_store
from .auth import groups_of
from .permissions import may


def attention(request):
    staff = getattr(request, "staff", None)
    if staff is None:
        return {}
    groups = groups_of(staff)
    counts = {}
    if may(groups, "booking.view"):
        counts["bookings"] = len(booking_store.staff_queue([BookingStatus.PENDING]))
    if may(groups, "question.view"):
        counts["questions"] = sum(1 for q in question_store.queue() if not q.is_answered)
    if may(groups, "request.view"):
        counts["requests"] = request_store.open_count()
    return {"attention": counts}


#: Masthead link -> the action that opens the page behind it.
#:
#: One table, so a menu item and the `@requires` on its view cannot drift apart. The
#: keys are what the template asks for, and `bookings`/`questions`/`requests` are spelled
#: the same as `attention`'s, so the two read together in the markup.
#:
#: The same idea as `views_auth.LANDING`, which resolves where `/api/staff/` sends
#: somebody. That one picks the first page they may read; this one picks all of them.
NAV_ACTIONS = {
    "cars": "car.view",
    "bookings": "booking.view",
    "slots": "slot.view",
    "schedules": "schedule.view",
    "questions": "question.view",
    "requests": "request.view",
    "customers": "customer.view",
    "staff": "staff.view",
}


def can(request):
    """Which masthead sections this person may open.

    The menu used to offer all eight to everybody, with a comment observing that a
    manager following the Staff link got a 403 rather than a broken page. True, and
    still a menu that lies: `staff.*` is `OWNER_ONLY`, so that item was never theirs.

    Worse, an account in the `staff` group and nothing else may open *nothing* --
    `GROUP_ACTIONS` gives that group no actions at all -- and one of those is a single
    unticked Roles box away on the add form. `views_auth.index` already tells such a
    person there is nothing to show; the masthead above it was offering eight links.

    Not a security control, and not treated as one: every view keeps its guard. This is
    only the menu agreeing with them.

    A second processor rather than more keys on `attention`, because `groups_of` is a
    set built from an attribute with no I/O -- it costs nothing -- and each of these
    then answers one question.
    """
    staff = getattr(request, "staff", None)
    if staff is None:
        return {}
    groups = groups_of(staff)
    return {"can": {name: may(groups, action)
                    for name, action in NAV_ACTIONS.items()}}
