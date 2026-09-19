"""Derivative generation scheduling.

Why this is inline, and what should change
------------------------------------------
Resizing happens inline, bounded by a time budget. API Gateway gives up at 29 seconds,
and a save that dies there is far worse than a photo briefly served at full size.
Anything unfinished is left pending and picked up by the next save, the staff action, or
`manage.py rebuild_derivatives`. Until then the API serves the original, so the only cost
of falling behind is bytes.

**This design is a leftover, and moving it is the last outstanding item from the DynamoDB
cutover.** The original reason was that the function ran in a private subnet with no NAT
gateway and only an S3 *gateway* endpoint: it had no network path to the Lambda API, so a
self-invoke did not fail fast -- it hung until the function timed out and surfaced as a
504. Buying a path meant an interface VPC endpoint at roughly $10/month across two AZs,
more than the entire site cost to run. A scheduled sweeper was out for a second reason:
every run would have queried Aurora, which only scaled to zero after ten idle minutes, so
polling would have kept it awake around the clock.

Both reasons are gone. There is no VPC and no Aurora, `lambda:InvokeFunction` is
reachable, and the budget and its `threading.local` deadline exist only to work around a
constraint that no longer applies. See `docs/INFRA.md`.
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
    from .store import images as image_store
    from .store.errors import NotFound

    if _budget_remaining() <= 0:
        logger.info("Budget spent; deferring derivatives for CarImage %s", car_image_id)
        return False

    car_id, _, image_id = str(car_image_id).partition(":")
    try:
        car_image = image_store.get(car_id, image_id)
    except NotFound:
        return False

    if not car_image.image_name or car_image.derivatives_ready:
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

    Called opportunistically from staff saves, so a backlog clears itself without anybody
    running the management command.
    """
    from .store import images as image_store

    done = 0
    # A sparse GSI partition, normally empty: the index attributes are removed when the
    # copies land, so this costs one small query rather than a scan for a flag.
    for car_image in image_store.pending_derivatives(limit=limit):
        if _budget_remaining() <= 0:
            break
        if not car_image.image_name:
            continue
        if build_derivatives_task(f"{car_image.car_id}:{car_image.image_id}"):
            done += 1
    return done
