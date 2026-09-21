"""Car requests: reads and writes, no wording.

One partition per request and one GSI1 partition for the whole list, the same shape
as questions. There are never enough of these to paginate -- a small shop gets a few
a week -- so the queue is one Query, filtered in Python.
"""

from django.utils import timezone

from ..choices import RequestStatus
from . import keys
from .errors import NotFound
from .models import CarRequest


def create(*, name, email, phone, details, language="en", customer_sub=None, now=None):
    now = now or timezone.now()
    request_id = keys.new_id()
    row = CarRequest(
        pk=keys.request_pk(request_id),
        sk=keys.META,
        request_id=request_id,
        name=name.strip(),
        email=email.strip().lower(),
        phone=phone.strip(),
        details=details.strip(),
        language="ja" if language == "ja" else "en",
        customer_sub=customer_sub or None,
        status=RequestStatus.OPEN,
        created_at=now,
        updated_at=now,
        gsi1pk=keys.REQUEST_GSI1PK,
        gsi1sk=keys.request_gsi1sk(now, request_id),
    )
    row.save()
    return row


def get(request_id):
    try:
        return CarRequest.get(keys.request_pk(request_id), keys.META)
    except CarRequest.DoesNotExist:
        raise NotFound(f"request {request_id}")


def queue(status=None, limit=None):
    """Every request, newest first; or only those in one status."""
    rows = list(CarRequest.gsi1.query(keys.REQUEST_GSI1PK, scan_index_forward=False))
    if status:
        rows = [r for r in rows if r.status == status]
    return rows[:limit] if limit else rows


def open_count():
    return len(queue(RequestStatus.OPEN))


def resolve(request, *, staff_sub, note="", now=None):
    """Mark a request dealt with. Guarded: an update must never create a stub."""
    now = now or timezone.now()
    request.update(
        actions=[
            CarRequest.status.set(RequestStatus.RESOLVED),
            CarRequest.resolved_at.set(now),
            CarRequest.resolved_by.set(staff_sub or ""),
            CarRequest.resolution_note.set((note or "").strip()),
            CarRequest.updated_at.set(now),
        ],
        condition=CarRequest.pk.exists(),
    )
    return request


def reopen(request, *, now=None):
    now = now or timezone.now()
    request.update(
        actions=[
            CarRequest.status.set(RequestStatus.OPEN),
            CarRequest.resolved_at.remove(),
            CarRequest.resolved_by.remove(),
            CarRequest.updated_at.set(now),
        ],
        condition=CarRequest.pk.exists(),
    )
    return request
