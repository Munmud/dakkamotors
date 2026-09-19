"""Car photo storage, and the denormalisation the listing page depends on.

`Car.primary_image_ref` is a copy of whichever photo a listing card should show. Without
it the car list would be one Query for the cars plus one per card for their images --
the N+1 that `prefetch_related("images")` used to absorb, and which has no equivalent
here.

The cost of that denormalisation is an invariant: **every path that could change which
photo is primary must call `refresh_primary`.** There are seven: create, delete, reorder,
flagging one primary, replacing a photo's file, derivatives becoming ready, and
`media.set_order`. (The docstring said five for as long as there were six; it is written
out one per line now so the next one added is visibly a change to this list.)

Six of the seven are in this module, which is the only reason the invariant is keepable.
The seventh is in `media.py` and cannot move here, because it reorders photos and videos
together and this module deliberately cannot see a video.
"""

from django.core.files.storage import default_storage

from . import keys
from .errors import NotFound
from .models import Car, CarImage


def _ref(image):
    if image is None:
        return None
    return {
        "sk": image.sk,
        "id": image.image_id,
        "name": image.image_name,
        "widths": image.available_widths,
        "ready": bool(image.derivatives_ready),
    }


def for_car(car_id):
    """Every photo on a car, in display order. Photos only: a `VID#` row cannot match.

    Sorted on `(order, sk)` rather than `(order, image_id)` so this list composes with the
    video list in `media.merge`. For a photo the two are the same ordering -- `IMG#` is a
    constant prefix -- which is what made the change safe to make under existing tests.
    """
    rows = list(CarImage.query(keys.car_pk(car_id),
                               range_key_condition=CarImage.sk.startswith("IMG#")))
    rows.sort(key=lambda i: (int(i.order or 0), i.sk or ""))
    return rows


def pick_primary(images):
    """Explicitly flagged wins; otherwise the first by display order.

    Same rule as the old `Car.primary_image` property, kept here so there is exactly one
    definition of "which photo represents this car".
    """
    if not images:
        return None
    for image in images:
        if image.is_primary:
            return image
    return images[0]


def refresh_primary(car_id):
    """Recompute the card reference on the car item.

    Cheap -- the images are already in the car's own partition -- and it is what keeps a
    listing card from showing a photo that has been deleted or reordered away.
    """
    images = for_car(car_id)
    ref = _ref(pick_primary(images))
    try:
        car = Car.get(keys.car_pk(car_id), keys.META)
    except Car.DoesNotExist:
        raise NotFound(f"no car {car_id!r}") from None
    car.update(actions=[
        Car.primary_image_ref.set(ref) if ref else Car.primary_image_ref.remove()
    ])
    return ref


def create(*, car_id, image_name, is_primary=False, order=0, now=None):
    image_id = keys.new_id()
    image = CarImage(
        pk=keys.car_pk(car_id),
        sk=keys.image_sk(image_id),
        image_id=image_id,
        car_id=car_id,
        image_name=image_name,
        is_primary=bool(is_primary),
        order=int(order),
        derivatives_ready=False,
        created_at=now,
        # Sparse GSI1 entry: this is how tasks.process_pending finds photos whose
        # resized copies never got made. The attributes are removed when the
        # derivatives land, so the partition is normally empty.
        gsi1pk=keys.IMAGE_PENDING_GSI1PK,
        gsi1sk=image_id,
    )
    image.save()
    if is_primary:
        _unset_other_primaries(car_id, image_id)
    refresh_primary(car_id)
    return image


def _unset_other_primaries(car_id, keep_image_id):
    """Only the first one counts, as the old help text put it."""
    for other in for_car(car_id):
        if other.image_id != keep_image_id and other.is_primary:
            other.update(actions=[CarImage.is_primary.set(False)])


def get(car_id, image_id):
    try:
        return CarImage.get(keys.car_pk(car_id), keys.image_sk(image_id))
    except CarImage.DoesNotExist:
        raise NotFound(f"no image {image_id!r}") from None


def set_order(car_id, ordering):
    """`ordering` is a list of image ids, in the order staff arranged them."""
    for position, image_id in enumerate(ordering):
        get(car_id, image_id).update(actions=[CarImage.order.set(position)])
    refresh_primary(car_id)


def set_primary(car_id, image_id):
    image = get(car_id, image_id)
    image.update(actions=[CarImage.is_primary.set(True)])
    _unset_other_primaries(car_id, image_id)
    refresh_primary(car_id)
    return image


def mark_derivatives_ready(car_id, image_id, widths):
    """Record the resized copies, and drop the item out of the pending index.

    Removing `gsi1pk`/`gsi1sk` rather than setting them to some "done" value is what
    keeps the pending partition sparse -- an item with no index key simply is not in the
    index.
    """
    image = get(car_id, image_id)
    image.update(actions=[
        CarImage.derivatives_ready.set(True),
        CarImage.derivative_widths.set([int(w) for w in widths]),
        CarImage.gsi1pk.remove(),
        CarImage.gsi1sk.remove(),
    ])
    refresh_primary(car_id)
    return image


def pending_derivatives(limit=None):
    rows = list(CarImage.gsi1.query(keys.IMAGE_PENDING_GSI1PK))
    return rows[:limit] if limit else rows


def replace_file(car_id, image_id, image_name):
    """A new photo on an existing row invalidates the old resized copies.

    Stale derivatives describe the previous photo, so they stop being served
    immediately -- the same reasoning the old `CarImage.save()` had, made explicit
    because there is no `save()` hook to hide it in.
    """
    image = get(car_id, image_id)
    image.update(actions=[
        CarImage.image_name.set(image_name),
        CarImage.derivatives_ready.set(False),
        CarImage.derivative_widths.remove(),
        CarImage.gsi1pk.set(keys.IMAGE_PENDING_GSI1PK),
        CarImage.gsi1sk.set(image_id),
    ])
    refresh_primary(car_id)
    return image


def delete(car_id, image_id, *, delete_files=True):
    """Remove a photo row and, by default, the files behind it.

    Django left files on disk by default and `models.py` hung a `post_delete` receiver
    off CarImage to clean them up. There are no signals here, so the cleanup is an
    explicit argument -- visible at the call site, and switchable off for the migration
    importer, which must not touch S3.
    """
    image = get(car_id, image_id)
    names = []
    if delete_files and image.image_name:
        names = [image.image_name] + [
            image.derivative_name(w) for w in image.available_widths
        ]

    image.delete()
    refresh_primary(car_id)

    for name in names:
        try:
            default_storage.delete(name)
        except Exception:  # noqa: BLE001 - cleanup must never break the delete itself
            pass
    return len(names)
