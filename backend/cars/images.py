"""Derivative generation for uploaded car photos.

The only place images are resized. The admin's direct-to-S3 upload puts the original
in place untouched; everything the site actually serves is produced here.

Kept deliberately free of Django request handling so it can be called from a management
command, a test, or an asynchronous Lambda invocation without changing shape.
"""

import io
import logging

from django.core.files.base import ContentFile
from PIL import Image, ImageOps

from .models import DERIVATIVE_WIDTHS

logger = logging.getLogger(__name__)

WEBP_QUALITY = 82

# Encoder effort. 6 is the slowest setting; 4 is several times faster for roughly 1-2%
# more bytes, which is the right trade when this runs inside a request with a time
# budget rather than in a background job.
WEBP_METHOD = 4


def _resized(source, width):
    """A copy of `source` scaled to `width`, preserving aspect ratio."""
    height = round(source.height * (width / source.width))
    return source.resize((width, height), Image.Resampling.LANCZOS)


def build_derivatives(car_image, widths=DERIVATIVE_WIDTHS):
    """Generate WebP copies of one CarImage and record which ones exist.

    Returns the list of widths written. Widths larger than the original are skipped:
    upscaling costs bytes and adds no detail, and claiming a 1600px copy that is really
    a blurred 900px one would make `srcset` pick the wrong file.
    """
    storage = car_image.image.storage

    with car_image.image.open("rb") as fh:
        source = Image.open(fh)
        # Phone cameras record orientation in EXIF rather than rotating the pixels, so
        # without this a portrait photo is served on its side.
        source = ImageOps.exif_transpose(source)
        # WebP has no CMYK support and alpha would be discarded silently on flatten.
        if source.mode not in ("RGB", "RGBA"):
            source = source.convert("RGB")
        source.load()

    written = []
    for width in sorted(widths):
        if width > source.width:
            continue

        buffer = io.BytesIO()
        _resized(source, width).save(buffer, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
        buffer.seek(0)

        name = car_image.derivative_name(width)
        if storage.exists(name):
            storage.delete(name)
        storage.save(name, ContentFile(buffer.read()))
        written.append(width)

    # An original narrower than the smallest target still needs something to serve, so
    # fall back to a single copy at the original width rather than leaving none.
    if not written:
        buffer = io.BytesIO()
        source.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
        buffer.seek(0)
        name = car_image.derivative_name(source.width)
        if storage.exists(name):
            storage.delete(name)
        storage.save(name, ContentFile(buffer.read()))
        written.append(source.width)

    car_image.derivative_widths = ",".join(str(w) for w in written)
    car_image.derivatives_ready = True
    car_image.save(update_fields=["derivative_widths", "derivatives_ready"])

    logger.info("Built derivatives for CarImage %s: %s", car_image.pk, written)
    return written
