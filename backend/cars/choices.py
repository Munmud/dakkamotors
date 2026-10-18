"""Enumerations, shared by everything that needs them.

Extracted during the DynamoDB migration so the ORM models and the store could not drift
apart while both existed. The ORM is gone; this is simply where the enums live now, read
by `cars/store/`, `cars/staff/forms.py` and the management commands.

`TextChoices` needs no database -- it is an enum with labels -- which is what makes it
safe to import from anywhere, including code running with `DATABASES = {}`.
"""

from django.db import models


class FuelType(models.TextChoices):
    PETROL = "petrol", "Petrol"
    # Beside Petrol rather than instead of it, at the owner's choice. The two are the
    # same fuel; existing cars say "petrol", and the shop wanted the word its customers
    # use available for new ones without a backfill.
    GASOLINE = "gasoline", "Gasoline"
    DIESEL = "diesel", "Diesel"
    HYBRID = "hybrid", "Hybrid"
    ELECTRIC = "electric", "Electric"
    LPG = "lpg", "LPG"


class CarStatus(models.TextChoices):
    AVAILABLE = "available", "Available"
    RESERVED = "reserved", "Reserved"
    SOLD = "sold", "Sold"


class RequestStatus(models.TextChoices):
    """A lead. Open until somebody has rung the person back and dealt with it."""

    OPEN = "open", "Open"
    RESOLVED = "resolved", "Resolved"


#: The widths every uploaded photo is resized to, in WebP.
#:
#: **Pick these by working out which file a browser actually chooses**, which is the
#: smallest candidate at least as wide as (CSS pixels x device pixel ratio). Round
#: numbers that look well spaced are not the same thing, and the previous set proved
#: it: 320/640/1000/1600 was chosen to close "the gap in the middle" and did the
#: opposite, because 1000 is still below what the case it was aimed at needs.
#:
#:   context                    DPR  needs   320/800/1600   320/640/1000/1600
#:   card or hero, phone 390     2     780      800            1000   <- heavier
#:   card or hero, phone 390     3    1170     1600            1600   <- unchanged
#:   hero, desktop               1     700      800            1000   <- heavier
#:
#: So it made the commonest case bigger and fixed nothing. 1200 is the width that
#: case actually wants: a 390pt phone at 3x asks for 1170 and now gets 1200 instead
#: of 1600, roughly 40% fewer bytes, and a phone advertisement is most of this site's
#: traffic. 800 stays because it is exactly right at 2x, and keeping it means the
#: copies already in S3 stay valid rather than becoming orphans.
#:
#: 320 is the gallery thumbnail, pinned. 1600 is the widest thing this site serves --
#: the lightbox, and a desktop hero at 2x -- and the original is never handed to a
#: browser at all.
#:
#: **Changing this tuple is not a code change on its own.** Every photo already in S3
#: keeps the widths it was built with, so the site goes on serving the old set while
#: the code claims the new one; `manage.py rebuild_derivatives --all` is what makes
#: them agree, followed by a CDN invalidation. That gap sat in production unnoticed
#: once already.
DERIVATIVE_WIDTHS = (320, 800, 1200, 1600)
