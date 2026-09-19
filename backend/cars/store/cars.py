"""Car storage, including the two uniqueness guards and the single-Query detail read.

Two things here are worth reading before changing anything:

**The slug guard doubles as the lookup.** `SLUG#<slug>` is both the uniqueness constraint
and the slug -> car_id index, so enforcing uniqueness costs nothing extra and the public
`/cars/<slug>` path is one GetItem.

**Slug collisions are resolved by the database, not by a loop.** The old
`Car.build_slug()` looped with `.exists()` until it found a free candidate, which is
check-then-write: two simultaneous saves of identically-named cars could both pass the
check and one would fail on the unique index -- or, worse, on SQLite locally, both could
succeed. Here the guard is a conditional put, so a collision is *reported* rather than
raced, and we simply retry with a bumped suffix.
"""

import itertools

from . import keys
from .errors import ConditionFailed, NotFound
from .models import (
    Car, CarImage, CarQuestion, ChassisGuard, LegacyCarPointer, SlugGuard,
)
from .txn import Txn, failed

SLUG, CHASSIS, CAR, LEGACY = "slug", "chassis", "car", "legacy"

#: How many suffixes to try before giving up. Ten identically-named cars in one
#: inventory would be remarkable; a runaway loop would not be.
MAX_SLUG_ATTEMPTS = 10

# slug -> car_id, memoised for the life of a warm container. Safe because a slug is
# generated once and then left alone -- models.py is explicit that changing one breaks
# every link already shared. Invalidated on delete, and on the rare deliberate re-slug.
_slug_cache = {}


def create(car, *, now):
    """Write a car and both of its guards atomically.

    Either the car and its slug and chassis guards all land, or none do. That matters:
    an orphaned slug guard would permanently reserve a URL for a car that does not
    exist, and nothing would ever clean it up.
    """
    car.created_at = car.created_at or now
    car.updated_at = now
    car.search_blob = car.build_search_blob()
    base = car.build_slug_base()

    for candidate in _slug_candidates(base):
        car.slug = candidate
        car.pk = keys.car_pk(car.car_id)
        car.sk = keys.META
        car.gsi1pk = keys.car_status_gsi1pk(car.status)
        car.gsi1sk = keys.car_gsi1sk(car.created_at, car.car_id)

        tx = Txn()
        tx.save(CAR, car, condition=Car.pk.does_not_exist())
        tx.save(SLUG,
                SlugGuard(pk=keys.slug_pk(candidate), sk="SLUG", car_id=car.car_id),
                condition=SlugGuard.pk.does_not_exist())
        if car.chassis_number:
            tx.save(CHASSIS,
                    ChassisGuard(pk=keys.chassis_guard_pk(car.chassis_number),
                                 sk=keys.GUARD, car_id=car.car_id),
                    condition=ChassisGuard.pk.does_not_exist())
        order = tx.labels_in_wire_order()
        try:
            tx.commit()
        except Exception as exc:
            if getattr(exc, "reasons", None) is None:
                raise
            if CHASSIS in order and failed(exc, CHASSIS, order):
                raise ConditionFailed(
                    f"chassis number {car.chassis_number!r} is already on another car"
                ) from exc
            if failed(exc, SLUG, order):
                continue  # somebody took that slug; try the next suffix
            if failed(exc, CAR, order):
                raise ConditionFailed(f"car {car.car_id!r} already exists") from exc
            raise
        _slug_cache[candidate] = car.car_id
        return car

    raise ConditionFailed(
        f"could not find a free slug for {base!r} after {MAX_SLUG_ATTEMPTS} attempts"
    )


def _slug_candidates(base):
    yield base
    for suffix in itertools.islice(itertools.count(2), MAX_SLUG_ATTEMPTS - 1):
        yield f"{base}-{suffix}"


def get(car_id):
    try:
        return Car.get(keys.car_pk(car_id), keys.META)
    except Car.DoesNotExist:
        raise NotFound(f"no car {car_id!r}") from None


def find(car_id):
    try:
        return get(car_id)
    except NotFound:
        return None


def car_id_for_slug(slug):
    if slug in _slug_cache:
        return _slug_cache[slug]
    try:
        guard = SlugGuard.get(keys.slug_pk(slug), "SLUG")
    except SlugGuard.DoesNotExist:
        return None
    _slug_cache[slug] = guard.car_id
    return guard.car_id


def by_slug(slug):
    car_id = car_id_for_slug(slug)
    return find(car_id) if car_id else None


def slug_for_legacy_id(numeric_id):
    """`/cars/34` still has to 301 somewhere after the ids change."""
    try:
        return LegacyCarPointer.get(keys.legacy_car_pk(numeric_id), keys.META).slug
    except LegacyCarPointer.DoesNotExist:
        return None


def detail(car_id):
    """The car, its images and its published questions -- in ONE Query.

    This is the whole reason the design is single-table. The page is server rendered for
    crawlers and link previews on a possibly-cold Lambda, so collapsing three sequential
    round trips into one is the latency that actually matters.

    The sort keys guarantee the order IMG# < META < Q#, so the partition arrives already
    grouped and nothing needs sorting afterwards.
    """
    from .base import BaseItem

    car, images, questions = None, [], []
    for item in BaseItem.query(keys.car_pk(car_id)):
        if isinstance(item, Car):
            car = item
        elif isinstance(item, CarImage):
            images.append(item)
        elif isinstance(item, CarQuestion):
            questions.append(item)

    if car is None:
        raise NotFound(f"no car {car_id!r}")

    images.sort(key=lambda i: (int(i.order or 0), i.image_id or ""))
    car.images = images
    car.questions = questions
    return car


def detail_by_slug(slug):
    car_id = car_id_for_slug(slug)
    if not car_id:
        raise NotFound(f"no car with slug {slug!r}")
    return detail(car_id)


def list_by_status(status, limit=None):
    """Cars of one status, newest first.

    Read whole and sliced in Python rather than paginated with a cursor: the inventory
    is tens of cars, one Query is a single 1 MB page, and keeping `?page=N` with an
    accurate `count` means the React app needs no change at all. The ceiling is a few
    thousand cars -- switch to LastEvaluatedKey then, not before.
    """
    rows = list(Car.gsi1.query(keys.car_status_gsi1pk(status),
                               scan_index_forward=False))
    return rows[:limit] if limit else rows


def for_sitemap(statuses=("available", "reserved")):
    """Everything a sitemap may advertise. Not a Scan -- two Queries.

    `ProjectionExpression` would trim the wire further, but the descriptions are the
    only large attributes and at this inventory size the saving is noise.
    """
    out = []
    for status in statuses:
        out.extend(Car.gsi1.query(keys.car_status_gsi1pk(status)))
    out.sort(key=lambda c: c.updated_at or c.created_at, reverse=True)
    return out


def update(car, *, now, **fields):
    """Apply field changes, keeping the derived attributes in step.

    Changing `status` moves the car between GSI1 partitions; changing the chassis number
    moves its guard. Both are handled here so no caller has to remember.
    """
    old_status = car.status
    old_chassis = car.chassis_number

    for name, value in fields.items():
        setattr(car, name, value)
    car.updated_at = now
    car.search_blob = car.build_search_blob()
    if car.status != old_status:
        car.gsi1pk = keys.car_status_gsi1pk(car.status)

    chassis_changed = car.chassis_number != old_chassis
    if not chassis_changed:
        car.save()
        return car

    tx = Txn()
    tx.save(CAR, car)
    if car.chassis_number:
        tx.save(CHASSIS,
                ChassisGuard(pk=keys.chassis_guard_pk(car.chassis_number),
                             sk=keys.GUARD, car_id=car.car_id),
                condition=ChassisGuard.pk.does_not_exist())
    if old_chassis:
        tx.delete(CHASSIS + "_old",
                  ChassisGuard(pk=keys.chassis_guard_pk(old_chassis), sk=keys.GUARD))
    order = tx.labels_in_wire_order()
    try:
        tx.commit()
    except Exception as exc:
        if getattr(exc, "reasons", None) is not None and failed(exc, CHASSIS, order):
            raise ConditionFailed(
                f"chassis number {car.chassis_number!r} is already on another car"
            ) from exc
        raise
    return car


def bump_updated_at(car_id, now):
    """Move the sitemap's lastmod without touching anything else.

    Conditional on the car existing, for the reason spelled out in
    `store/questions.publish`: an unconditional UpdateItem is an upsert, and the stub
    it would create is invisible to this package's polymorphic reads.
    """
    car = get(car_id)
    car.update(actions=[Car.updated_at.set(now)], condition=Car.pk.exists())
    return car


def delete(car):
    """Remove a car, its guards, its legacy pointer, and its children.

    Deliberately in two steps. The guarded core goes atomically, because a car whose
    slug guard outlived it would permanently reserve that URL. The children then go as
    a best-effort batch: `TransactWriteItems` caps at 100 items and a car with many
    photos and a long question thread could exceed it.

    The legacy pointer is in the atomic half for the same reason as the slug guard, and
    it is the one that bites hardest if forgotten: `config/urls.py` reads it to turn an
    old numeric URL into a **301**. A pointer outliving its car sends every visitor and
    crawler permanently to a slug that no longer resolves -- and a cached 301 to a 404 is
    worse than the 404, because nothing asks again. Falling through to `/` is the
    intended behaviour once the car is gone.

    Cars created after the migration have no pointer; `DeleteItem` on a key that is not
    there succeeds, so this needs no condition.

    Orphaned children are invisible -- nothing queries a deleted car's partition -- and
    the batch below is best-effort rather than guaranteed. S3 cleanup is the caller's job
    now that there is no `post_delete` signal to hang it on.
    """
    from .base import BaseItem

    tx = Txn()
    tx.delete(CAR, Car(pk=car.pk, sk=keys.META))
    if car.slug:
        tx.delete(SLUG, SlugGuard(pk=keys.slug_pk(car.slug), sk="SLUG"))
    if car.chassis_number:
        tx.delete(CHASSIS, ChassisGuard(pk=keys.chassis_guard_pk(car.chassis_number),
                                        sk=keys.GUARD))
    tx.delete(LEGACY, LegacyCarPointer(pk=keys.legacy_car_pk(car.car_id),
                                       sk=keys.META))
    tx.commit()
    _slug_cache.pop(car.slug, None)

    removed = 0
    with BaseItem.batch_write() as batch:
        for item in BaseItem.query(keys.car_pk(car.car_id)):
            batch.delete(item)
            removed += 1
    return removed


def forget_slug(slug):
    """Drop a memoised slug. Only needed when a slug is deliberately changed."""
    _slug_cache.pop(slug, None)


def clear_slug_cache():
    _slug_cache.clear()
