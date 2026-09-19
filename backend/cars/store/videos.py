"""Car video storage.

Deliberately thinner than `images.py`, and the difference is the point. A photo carries a
denormalisation (`Car.primary_image_ref`) that every mutation has to maintain, and a set
of resized copies that have to be generated, tracked and swept. A video carries neither:
nothing transcodes it, nothing indexes it, and it can never be the listing card photo --
`images.for_car` queries `begins_with(sk, "IMG#")`, so a `VID#` row is invisible to the
code that picks one.

What a video *does* share is `order`, one number space with photos, because staff order
one gallery rather than two lists. That merge lives in `media.py`; nothing here knows
about photos.
"""

from django.core.files.storage import default_storage

from . import keys
from .errors import NotFound
from .models import CarVideo


def for_car(car_id):
    """Every video on a car, in display order.

    Sorted on `(order, sk)` rather than `(order, video_id)` so it composes with the photo
    list in `media.gallery` -- same key, two item types, one sequence. Ties fall back to
    the id inside the sort key, which is creation-ordered.
    """
    rows = list(CarVideo.query(keys.car_pk(car_id),
                               range_key_condition=CarVideo.sk.startswith("VID#")))
    rows.sort(key=lambda v: (int(v.order or 0), v.sk or ""))
    return rows


def get(car_id, video_id):
    try:
        return CarVideo.get(keys.car_pk(car_id), keys.video_sk(video_id))
    except CarVideo.DoesNotExist:
        raise NotFound(f"no video {video_id!r} on car {car_id!r}") from None


def create(*, car_id, video_name, order=0, now=None):
    """Attach a video. The file is already in S3 by the time this is called.

    No condition and no guard: a video has no uniqueness to protect, and two rows
    pointing at the same object would be odd but harmless. Contrast `cars.create`, where
    the slug and chassis guards are the whole reason that write is a transaction.
    """
    video = CarVideo(
        pk=keys.car_pk(car_id),
        sk=keys.video_sk(keys.new_id()),
        car_id=car_id,
        video_name=video_name,
        order=int(order or 0),
        created_at=now,
    )
    video.video_id = video.sk.split("#", 1)[1]
    video.save()
    return video


def delete(car_id, video_id, *, delete_files=True):
    """Remove a video and, by default, the object it points at.

    File deletion is best-effort and deliberately after the row: an orphaned S3 object
    costs a fraction of a cent and is findable, whereas a row pointing at a file that is
    already gone renders a broken player on the car page.
    """
    video = get(car_id, video_id)
    name = video.video_name
    video.delete()

    if delete_files and name:
        try:
            default_storage.delete(name)
        except Exception:  # noqa: BLE001 - a missing object is not an error here
            pass
    return name
