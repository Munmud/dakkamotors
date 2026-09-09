"""Background work, run out of band from the request that triggered it.

Resizing a handful of multi-megabyte photos takes longer than API Gateway's hard
29-second integration timeout allows, so it must not happen while the admin's save
request is still open. Zappa's `task` decorator invokes the same Lambda a second time,
asynchronously; outside Lambda it simply calls the function inline, so `runserver` and
the test suite need no special handling.
"""

import logging

logger = logging.getLogger(__name__)

try:
    from zappa.asynchronous import task
except ImportError:  # pragma: no cover - zappa is absent in some local setups
    def task(func):
        """Inline stand-in so local development behaves like production, just slower."""
        func.sync = func
        return func


@task
def build_derivatives_task(car_image_id):
    """Generate the WebP copies for one photo.

    Takes an id rather than a model instance because the arguments are serialised into
    a Lambda invocation payload.
    """
    from .images import build_derivatives
    from .models import CarImage

    try:
        car_image = CarImage.objects.get(pk=car_image_id)
    except CarImage.DoesNotExist:
        # The photo was deleted between upload and processing. Nothing to repair.
        logger.info("CarImage %s vanished before processing", car_image_id)
        return

    try:
        build_derivatives(car_image)
    except Exception:
        # Leave derivatives_ready False so the API keeps serving the original and
        # `manage.py rebuild_derivatives` can pick this up later.
        logger.exception("Failed to build derivatives for CarImage %s", car_image_id)
        raise
