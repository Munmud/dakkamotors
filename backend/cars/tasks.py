"""Derivative generation scheduling.

Why this is not a background job
--------------------------------
The obvious design is to hand resizing to an asynchronous Lambda invocation. It cannot
work here: the function runs in a private subnet with no NAT gateway and only an S3
*gateway* endpoint, so it has no network path to the Lambda API and a self-invoke hangs
until the function times out. Giving it one means an interface VPC endpoint at roughly
$10/month across two AZs - more than the entire site costs to run.

A scheduled sweeper is out for a different reason: every run would query the database,
and Aurora Serverless v2 only scales to zero after ten idle minutes. Polling would keep
it awake permanently and quietly undo the thing that makes the database nearly free.

So resizing happens inline, bounded by a time budget. API Gateway gives up at 29
seconds, and a save that dies there is far worse than a photo that is briefly served at
full size. Anything not finished inside the budget is left pending and picked up by the
next save, the admin action, or `manage.py rebuild_derivatives`. Until then the API
serves the original, so the only cost of falling behind is bytes.
"""

import logging
import threading
import time

from django.core.signals import request_started
from django.dispatch import receiver

logger = logging.getLogger(__name__)

# Comfortably inside API Gateway's 29s ceiling, with room for the rest of the save.
BUDGET_SECONDS = 15

_state = threading.local()


@receiver(request_started)
def _reset_budget(**kwargs):
    """Give every request a fresh budget.

    Lambda reuses warm containers, so without this the first request to exhaust the
    budget would poison every later one in the same container.
    """
    _state.deadline = None


def _budget_remaining():
    now = time.monotonic()
    deadline = getattr(_state, "deadline", None)
    if deadline is None:
        _state.deadline = now + BUDGET_SECONDS
        return BUDGET_SECONDS
    return deadline - now


def build_derivatives_task(car_image_id):
    """Resize one photo now, if this request still has time for it.

    Returns True when the copies were built, False when it was deferred.
    """
    from .images import build_derivatives
    from .models import CarImage

    if _budget_remaining() <= 0:
        logger.info("Budget spent; deferring derivatives for CarImage %s", car_image_id)
        return False

    try:
        car_image = CarImage.objects.get(pk=car_image_id)
    except CarImage.DoesNotExist:
        return False

    if not car_image.image or car_image.derivatives_ready:
        return False

    try:
        build_derivatives(car_image)
        return True
    except Exception:
        # Left pending on purpose: derivatives_ready stays False, the API keeps serving
        # the original, and a later pass can retry.
        logger.exception("Failed to build derivatives for CarImage %s", car_image_id)
        return False


def process_pending(limit=10):
    """Catch up on photos left behind, while the budget lasts.

    Called opportunistically from admin saves, where the database is already awake, so
    it costs nothing extra in Aurora time.
    """
    from .models import CarImage

    done = 0
    pending = CarImage.objects.filter(derivatives_ready=False).exclude(image="")
    for car_image in pending[:limit]:
        if _budget_remaining() <= 0:
            break
        if build_derivatives_task(car_image.pk):
            done += 1
    return done
