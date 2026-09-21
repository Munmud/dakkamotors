"""What is waiting, for the masthead of every staff page.

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
