"""Every partition and sort key in the table, built in exactly one place.

Single-table design trades a readable schema for a single round trip, and the way that
trade goes wrong is a key string built inline somewhere far from here. Nothing outside
this module may concatenate a `pk` or an `sk`.

Two sort-key orderings are load-bearing, and both fall out of plain ASCII:

    within CAR#<id>    IMG#  <  META  <  Q#  <  VID#
    within CUST#<sub>  BOOKING#  <  DEDUPE#  <  NOTIF#  <  PROFILE
    within SLOT#<id>   LIVE#  <  META

The first is why the car detail page is one Query instead of three: images, then the car
itself, then its published questions, in that order, from one partition. Changing any
prefix here without re-checking those orderings will quietly break that.

`VID#` sorting last is harmless rather than lucky: `store/cars.detail()` queries the
partition with no range condition, so it reads every prefix whatever the order, and the
grouping is done in Python. What the prefix does buy is that photos and videos are each
one contiguous `begins_with` range -- which is what keeps `images.for_car` from ever
returning a video, and therefore what stops a video becoming the listing card photo.
"""

import datetime as dt
import secrets

# --------------------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------------------


def new_id():
    """A sortable, dependency-free identifier.

    13 digits of epoch milliseconds followed by 10 hex characters. Lexicographic order
    matches creation order, which is what lets ids ride inside a sort key. A ULID would
    do the same job; this is 23 characters and needs no package.
    """
    millis = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    return f"{millis:013d}{secrets.token_hex(5)}"


def car_pk(car_id):
    return f"CAR#{car_id}"


#: Sort keys that are a constant rather than a builder.
META = "META"
GUARD = "UNIQ"


def image_sk(image_id):
    return f"IMG#{image_id}"


def video_sk(video_id):
    return f"VID#{video_id}"


def request_pk(request_id):
    """A car request -- somebody asking the shop to find them a car.

    Its own partition, like a customer: it belongs to no car, and a guest may leave one
    with no account to hang it on. Listed through GSI1 under REQUEST_GSI1PK.
    """
    return f"REQ#{request_id}"


def slug_pk(slug):
    """Doubles as the slug -> car lookup, so uniqueness costs nothing extra."""
    return f"SLUG#{slug}"


def chassis_guard_pk(chassis_number):
    return f"UNIQ#CHASSIS#{chassis_number.strip().upper()}"


def legacy_car_pk(numeric_id):
    """Pointer kept so `/cars/34` can still 301 to the slug after the migration."""
    return f"CARID#{numeric_id}"


# --------------------------------------------------------------------------------------
# GSI1 keys
# --------------------------------------------------------------------------------------


def car_status_gsi1pk(status):
    return f"CAR#STATUS#{status}"


def car_gsi1sk(created_at, car_id):
    return f"{iso(created_at)}#{car_id}"


#: Constant GSI1 partitions. A single key each, which is fine at this shop's volume --
#: a few leads a week, and a photo queue that is empty in the steady state.
REQUEST_GSI1PK = "REQUEST"
IMAGE_PENDING_GSI1PK = "IMG#PENDING"


def request_gsi1sk(created_at, request_id):
    return f"{iso(created_at)}#{request_id}"


def iso(when):
    """Fixed-width UTC, matching PynamoDB's own UTCDateTimeAttribute serialisation.

    Width matters more than readability here: these strings are embedded in sort keys
    and compared with BETWEEN, and `datetime.isoformat()` drops microseconds when they
    happen to be zero. A 26-character timestamp sorting against a 31-character one is a
    bug that only shows up on the round second.
    """
    if when.tzinfo is None:
        raise ValueError("refusing to serialise a naive datetime")
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+0000")