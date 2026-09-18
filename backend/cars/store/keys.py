"""Every partition and sort key in the table, built in exactly one place.

Single-table design trades a readable schema for a single round trip, and the way that
trade goes wrong is a key string built inline somewhere far from here. Nothing outside
this module may concatenate a `pk` or an `sk`.

Two sort-key orderings are load-bearing, and both fall out of plain ASCII:

    within CAR#<id>    IMG#  <  META  <  Q#
    within CUST#<sub>  BOOKING#  <  DEDUPE#  <  NOTIF#  <  PROFILE
    within SLOT#<id>   LIVE#  <  META

The first is why the car detail page is one Query instead of three: images, then the car
itself, then its published questions, in that order, from one partition. Changing any
prefix here without re-checking those orderings will quietly break that.
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


def slot_id(schedule_id, starts_at):
    """Deterministic, and therefore *is* the `unique_slot_per_rule_occurrence` constraint.

    Because the id is derived from (schedule, start time), a conditional put on this key
    is `get_or_create` with no read and no race. It is also URL-safe, so it survives in
    a booking payload.
    """
    return f"{schedule_id}-{starts_at:%Y%m%dT%H%M}"


# --------------------------------------------------------------------------------------
# Partition keys
# --------------------------------------------------------------------------------------

def car_pk(car_id):
    return f"CAR#{car_id}"


def slot_pk(sid):
    return f"SLOT#{sid}"


def schedule_pk(schedule_id):
    return f"SCHED#{schedule_id}"


def customer_pk(sub):
    return f"CUST#{sub}"


# --------------------------------------------------------------------------------------
# Sort keys
# --------------------------------------------------------------------------------------

META = "META"
PROFILE = "PROFILE"
GUARD = "UNIQ"

def image_sk(image_id):
    return f"IMG#{image_id}"


def question_sk(created_at, question_id):
    return f"Q#{iso(created_at)}#{question_id}"


def seat_sk(sub):
    """The live-booking guard, living inside the slot's own item collection.

    Put here rather than in its own partition so that `Query(SLOT#<id>, begins_with
    "LIVE#")` is simultaneously the exact live-booking count (the reconciliation source
    of truth for `booked_count`) and the staff roster for that appointment, with the
    customer's phone number already on it.
    """
    return f"LIVE#{sub}"


def booking_sk(booking_id):
    return f"BOOKING#{booking_id}"


def notification_sk(created_at, notification_id):
    return f"NOTIF#{iso(created_at)}#{notification_id}"


def dedupe_sk(dedupe_key):
    return f"DEDUPE#{dedupe_key}"


# --------------------------------------------------------------------------------------
# Guard items -- uniqueness enforced by the primary key
# --------------------------------------------------------------------------------------

def slug_pk(slug):
    """Doubles as the slug -> car lookup, so uniqueness costs nothing extra."""
    return f"SLUG#{slug}"


def chassis_guard_pk(chassis_number):
    return f"UNIQ#CHASSIS#{chassis_number.strip().upper()}"


def rule_guard_pk(weekday, start_time, end_time):
    """`unique_test_drive_rule`, as a guard rather than as the schedule's own key.

    Tempting to make the natural key the partition key, but editing a rule's time would
    then change its identity -- and `slot_id` embeds `schedule_id`, so every future slot
    would be orphaned by a time correction.
    """
    return f"UNIQ#RULE#{weekday}#{start_time:%H%M}#{end_time:%H%M}"


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


def slot_month_gsi1pk(when):
    """Sharded by month so the slot partition never becomes a single hot key.

    The 28-day booking horizon spans at most two of these, so availability is one or two
    Queries. This is also the escape hatch to copy if a constant GSI1 partition below
    ever grows too warm.
    """
    return f"SLOT#{when:%Y-%m}"


def slot_gsi1sk(starts_at, sid):
    return f"{iso(starts_at)}#{sid}"


def booking_status_gsi1pk(status):
    return f"BOOKING#{status}"


def booking_gsi1sk(slot_starts_at, sid, booking_id):
    return f"{iso(slot_starts_at)}#{sid}#{booking_id}"


QUESTION_GSI1PK = "QUESTION"
SCHEDULE_GSI1PK = "SCHEDULE"
CUSTOMER_GSI1PK = "CUSTOMER"
IMAGE_PENDING_GSI1PK = "IMG#PENDING"


def question_gsi1sk(created_at, car_id, question_id):
    return f"{iso(created_at)}#{car_id}#{question_id}"


def schedule_gsi1sk(weekday, start_time, schedule_id):
    return f"{weekday}#{start_time:%H%M}#{schedule_id}"


# --------------------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------------------

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


# --------------------------------------------------------------------------------------
# Auth state (Cognito holds the identity; these hold what it does not)
# --------------------------------------------------------------------------------------

def pending_pk(email):
    """A sign-up that has not proved its address yet, keyed by the address itself.

    Cognito holds the UNCONFIRMED user and the password from the moment of sign-up; this
    item holds only what Cognito has nowhere to put -- the name, the phone number, where
    to send them afterwards, and which language to write in.
    """
    return f"PENDING#{email.strip().lower()}"


def pending_token_pk(token_hash):
    """The emailed link, stored as a hash.

    The raw token exists only in the email, so a database leak cannot hand somebody a
    working activation link. Same reasoning as the column it replaces.
    """
    return f"PENDTOK#{token_hash}"


def reset_token_pk(token_hash):
    return f"RESET#{token_hash}"


def reset_epoch_pk(sub):
    """Bumped on every successful reset.

    `PasswordResetTokenGenerator` derived its token from the password hash, so changing
    the password killed every outstanding link. Cognito never hands us the hash, so the
    same property is bought with a counter: a link carries the epoch it was minted
    under, and a reset moves it.
    """
    return f"RESETEPOCH#{sub}"


def legacy_password_pk(email):
    """A Django password hash, carried across so nobody has to reset.

    Read by the Cognito UserMigration trigger on a customer's first sign-in and deleted
    once they have been migrated. Everything here expires on a TTL regardless.
    """
    return f"LEGACYPW#{email.strip().lower()}"
