"""Tell CloudFront a page has changed.

The public pages are cached at the edge -- `/cars/<slug>` for five minutes by default
and up to fifteen, the JSON under `/api/cars` for one to five -- and the car page
embeds its own JSON as initial data, which the app trusts without refetching. So a
status changed in the staff pages could keep reading "Reserved" for a quarter of an
hour, and did. A staff save now asks the edge to forget the car, the listing and the
home page.

Deliberately fire-and-forget. A save must never fail, or wait long, because a CDN call
did: the worst outcome of a lost invalidation is the cache window we lived with
before. There is no distribution id locally or under test, and then this is a no-op,
which is also why no test needs a fake CloudFront.
"""

import logging
import time

import boto3
from django.conf import settings

logger = logging.getLogger(__name__)


def paths_for_car(car):
    """Everywhere a change to one car shows: its page, its JSON, and both listings."""
    return [
        f"/cars/{car.slug}*",
        f"/api/cars/{car.slug}*",
        "/api/cars/",
        "/api/cars/?*",
        "/",
        "/?*",
    ]


def invalidate(paths):
    distribution = getattr(settings, "CLOUDFRONT_DISTRIBUTION_ID", "")
    if not distribution or not paths:
        return False
    try:
        # us-east-1 is where CloudFront's control plane lives, whatever region the rest
        # of the stack is in; the default session region would be refused.
        boto3.client("cloudfront", region_name="us-east-1").create_invalidation(
            DistributionId=distribution,
            InvalidationBatch={
                "Paths": {"Quantity": len(paths), "Items": list(paths)},
                "CallerReference": f"{int(time.time() * 1000)}",
            },
        )
    except Exception:  # noqa: BLE001 - the page will refresh on its own in minutes
        logger.warning("CloudFront invalidation failed for %s", paths, exc_info=True)
        return False
    return True
