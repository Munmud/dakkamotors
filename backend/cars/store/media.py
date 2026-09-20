"""One gallery across two item types.

Staff arrange photos and videos in a single sequence -- a video may lead -- so "the
gallery" is a merge of two queries rather than a property of either. This module is the
only definition of that merge; `images.py` and `videos.py` each know only their own type,
which is what keeps the photo-only invariant in `images.py` provably photo-only.

The merge works because both modules sort on `(order, sk)` over one shared `order` number
space. Sorting on the sort key rather than the bare id is the part that makes two types
comparable: within a type the two sort identically, because `IMG#<id>` differs from `<id>`
by a constant prefix -- which is why changing it broke no existing test. Across types a
tie on `order` puts `IMG#` before `VID#`, arbitrary but stable, and only reachable for
rows staff have never ordered.
"""

from . import images, videos
from .models import CarImage, CarVideo

PHOTO = "photo"
VIDEO = "video"


def gallery(car_id):
    """Every photo and video on a car as `(kind, item)`, in display order.

    Two Queries against the same partition. `cars.detail` gets the same sequence from the
    single Query it already makes -- use that on the car page, and this where the caller
    has only an id.
    """
    return merge(images.for_car(car_id), videos.for_car(car_id))


def merge(photos, clips):
    """The ordering rule itself, separated so `cars.detail` can reuse it without a read."""
    items = [(PHOTO, item) for item in photos] + [(VIDEO, item) for item in clips]
    items.sort(key=lambda pair: (int(pair[1].order or 0), pair[1].sk or ""))
    return items


def next_order(car_id):
    """One past the last position, so a new row lands at the end of the gallery.

    `max() + 1` rather than `len()`: positions are only contiguous immediately after
    `set_order`, and `cars.create` never calls it. After a deletion `len()` would hand
    back a position another row is still sitting on, and the `(order, sk)` tie would
    then decide the sequence instead of the staff member.
    """
    items = gallery(car_id)
    return max((int(item.order or 0) for _, item in items), default=-1) + 1


def set_order(car_id, ordering):
    """Renumber the whole gallery. `ordering` is `[(kind, id), ...]` as staff arranged it.

    Positions are assigned across the merged list, not per type: numbering each type from
    zero would leave a photo and a video both at `order=0` and let the tiebreak, rather
    than the staff member, decide which one leads.

    The `refresh_primary` at the end is the reason this function exists here rather than
    in a view. Moving a video to the front shifts every photo's position, which changes
    `pick_primary`'s answer, which changes the listing card -- so reordering the gallery
    is a seventh path into an invariant that looks like it belongs to photos alone.
    """
    for position, (kind, item_id) in enumerate(ordering):
        if kind == VIDEO:
            videos.get(car_id, item_id).update(actions=[CarVideo.order.set(position)])
        else:
            images.get(car_id, item_id).update(actions=[CarImage.order.set(position)])
    images.refresh_primary(car_id)
