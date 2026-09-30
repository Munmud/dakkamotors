"""Tests for the DynamoDB store layer.

These run against **DynamoDB Local**, not moto. The correctness of the booking design
rests on four behaviours -- attribute-to-attribute comparison inside a condition, the
positional codes in CancellationReasons, ReturnValuesOnConditionCheckFailure returning
the offending item, and ValidationException on two operations against one item. Testing
those against a Python reimplementation would be testing the wrong artifact, and a false
green there is the worst outcome available.

Start one with:

    java -Djava.library.path=./DynamoDBLocal_lib -jar DynamoDBLocal.jar -inMemory -port 8123

and point DYNAMODB_ENDPOINT_URL at it. Without one, the whole module skips rather than
failing, so the suite still runs for someone who has not set it up.
"""

import contextlib
import datetime as dt
import unittest

from django.conf import settings
from django.test import SimpleTestCase
from pynamodb.constants import ALL_OLD
from pynamodb.exceptions import TransactWriteError

from .store import keys
from .store.base import BaseItem
from .store.errors import TransactionFailed
from .store.models import Car, CarImage, CarVideo, ChassisGuard
from .store.txn import Txn, connection, failed, reason_for

UTC = dt.timezone.utc


def _local_dynamo_available():
    if not getattr(settings, "DYNAMODB_ENDPOINT_URL", ""):
        return False
    try:
        BaseItem.exists()
        return True
    except Exception:
        return False


AVAILABLE = _local_dynamo_available()


@contextlib.contextmanager
def count_dynamo_calls():
    """How many round trips to DynamoDB a block costs.

    The replacement for `assertNumQueries`, and worth more than it was: the car detail
    page is deliberately a single Query over one partition -- images, the car and its
    published questions all sort into `IMG# < META < Q#` -- and the thing that would
    quietly undo that is a per-question read inside a loop. Counting operations is how
    that stays caught.

    Patches `Connection.dispatch` because every operation goes through it, so a new call
    site cannot slip past by using a different model class.
    """
    from pynamodb.connection.base import Connection

    seen = []
    original = Connection.dispatch

    def counting(self, operation_name, operation_kwargs):
        seen.append(operation_name)
        return original(self, operation_name, operation_kwargs)

    Connection.dispatch = counting
    try:
        yield seen
    finally:
        Connection.dispatch = original


def ensure_table():
    """Create the table if it is not there yet.

    Both entry points need this and neither may assume the other ran first: Django
    orders tests by module, so every `DynamoReset` class in `tests.py` runs before
    anything in this file. Leaning on that order is what let the suite pass against a
    container left over from an earlier run and fail against a fresh one -- the table
    was simply still there.
    """
    if not BaseItem.exists():
        BaseItem.create_table(wait=True)


def truncate_table():
    """Empty the table between tests, at the raw client.

    Deliberately not `BaseItem.scan()`. A polymorphic scan can only return items
    carrying the discriminator attribute, so anything written without one -- a stub left
    by an unconditional UpdateItem, say -- is invisible to it and would survive the
    truncation, then collide with a later test in a way that looks impossible. Dropping
    to the client means whatever is in the table goes.
    """
    client = connection().client
    table = BaseItem.Meta.table_name
    while True:
        page = client.scan(TableName=table, ConsistentRead=True,
                           ProjectionExpression="pk,sk")
        items = page.get("Items", [])
        if not items:
            return
        for item in items:
            client.delete_item(TableName=table,
                               Key={"pk": item["pk"], "sk": item["sk"]})


@unittest.skipUnless(
    AVAILABLE,
    "DynamoDB Local not reachable; set DYNAMODB_ENDPOINT_URL to run store tests",
)
class DynamoTestCase(SimpleTestCase):
    """Creates the table once, then truncates between tests.

    Truncating beats recreating: a table create/delete cycle is seconds even locally,
    and the whole suite would spend most of its time waiting on table status.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ensure_table()

    def setUp(self):
        super().setUp()
        truncate_table()


class TransactionWireOrderTests(DynamoTestCase):
    """The assumption the whole error-mapping design rests on.

    PynamoDB concatenates its four buckets -- ConditionCheck, Delete, Put, Update -- in
    a fixed order rather than preserving call order. If a future version changed that,
    every failure would still be *detected* but would be *explained* wrongly: a
    duplicate chassis number would be reported as a duplicate slug, `store.cars.create`
    would retry with a bumped suffix, and it would loop fifty times before giving up on
    the wrong thing. Silent, and staff-visible. So it is asserted against a captured
    request rather than trusted.

    The fixture used to be a booking -- slot, customer, seat, booking, four items over
    two buckets. Booking is gone; `store.cars.update` on a changed chassis number is
    the live transaction that still spans buckets, so it is the one asserted here.
    """

    def _capture(self):
        sent = []
        conn = connection()
        conn.client.meta.events.register(
            "provide-client-params.dynamodb.TransactWriteItems",
            lambda params, **kw: sent.append(params),
        )
        return sent

    def test_pynamodb_groups_transact_items_by_operation_type(self):
        sent = self._capture()

        tx = Txn()
        # Deliberately interleaved, and deliberately not in bucket order.
        tx.save("car", Car(pk=keys.car_pk("c1"), sk=keys.META, car_id="c1"))
        tx.delete("chassis_old", ChassisGuard(pk=keys.chassis_guard_pk("OLD-1"),
                                              sk=keys.GUARD))
        tx.save("chassis", ChassisGuard(pk=keys.chassis_guard_pk("NEW-1"),
                                        sk=keys.GUARD, car_id="c1"))
        tx.commit()

        wire = [list(item.keys())[0] for item in sent[-1]["TransactItems"]]
        self.assertEqual(
            wire, ["Delete", "Put", "Put"],
            "PynamoDB no longer groups TransactItems by operation type. "
            "store/txn.py's bucket order must be updated to match, or every "
            "transaction failure will be attributed to the wrong condition.",
        )
        self.assertEqual(
            tx.labels_in_wire_order(), ["chassis_old", "car", "chassis"],
            "Txn.labels_in_wire_order() disagrees with what actually went on the wire.",
        )

    def test_two_operations_on_one_item_is_rejected(self):
        """A constraint of the engine, not of any one caller.

        DynamoDB refuses a transaction that addresses the same item twice, and refuses
        the *whole* request rather than the second operation. Anything building a Txn
        over a set of keys that might overlap has to deduplicate before committing --
        which is why `store.cars.update` compares the old and new chassis guard keys
        before adding both.
        """
        car = Car(pk=keys.car_pk("c9"), sk=keys.META, car_id="c9")
        car.save()

        with self.assertRaises(TransactWriteError) as caught:
            tx = Txn()
            tx.update("a", car, actions=[Car.brand.set("One")])
            tx.update("b", car, actions=[Car.brand.set("Two")])
            tx.commit()
        cause = caught.exception.cause
        self.assertEqual(cause.response["Error"]["Code"], "ValidationException")


class CarDetailQueryTests(DynamoTestCase):
    """One Query, the whole car page.

    This is why the design is single-table at all. The car detail page is server
    rendered for crawlers and link previews on a possibly-cold Lambda, so three
    sequential round trips instead of one is the difference that matters.
    """

    def test_one_query_returns_images_the_car_and_videos_in_order(self):
        now = dt.datetime.now(UTC)
        pk = keys.car_pk("34")
        Car(pk=pk, sk=keys.META, car_id="34", brand="Toyota", model_name="Alphard",
            manufacture_year=2008, slug="2008-toyota-alphard",
            created_at=now, updated_at=now).save()
        CarImage(pk=pk, sk=keys.image_sk("001"), image_id="001",
                 image_name="cars/front.jpg", is_primary=True).save()
        CarImage(pk=pk, sk=keys.image_sk("002"), image_id="002",
                 image_name="cars/rear.jpg").save()
        CarVideo(pk=pk, sk=keys.video_sk("003"), video_id="003",
                 video_name="cars/walkaround.mp4", order=3).save()

        items = list(BaseItem.query(pk))
        # IMG# < META < VID# by sort key, which is what makes one Query enough: the
        # page needs all three and the discriminator sorts them out on the way back.
        self.assertEqual([type(i).__name__ for i in items],
                         ["CarImage", "CarImage", "Car", "CarVideo"])
        car = next(i for i in items if isinstance(i, Car))
        self.assertEqual(car.seo_title_plain, "2008 Toyota Alphard")
        self.assertEqual(car.get_absolute_url(), "/cars/2008-toyota-alphard")

    def test_a_subclass_query_sees_only_its_own_kind(self):
        pk = keys.car_pk("35")
        Car(pk=pk, sk=keys.META, car_id="35", brand="Honda").save()
        CarImage(pk=pk, sk=keys.image_sk("001"), image_id="001").save()
        self.assertEqual(len(list(Car.query(pk))), 1)
        self.assertEqual(len(list(CarImage.query(pk))), 1)


class TimestampTests(SimpleTestCase):
    """Timestamps are embedded in sort keys and compared with BETWEEN.

    `datetime.isoformat()` drops microseconds when they happen to be zero, so a
    26-character timestamp would sort against a 31-character one and a slot starting
    exactly on the second would fall out of a range query. Fixed width is not cosmetic.
    """

    def test_iso_is_fixed_width_regardless_of_microseconds(self):
        widths = {
            len(keys.iso(dt.datetime(2026, 3, 1, 9, 30, 0, micro, tzinfo=UTC)))
            for micro in (0, 1, 999999)
        }
        self.assertEqual(len(widths), 1)

    def test_iso_sorts_chronologically(self):
        a = keys.iso(dt.datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
        b = keys.iso(dt.datetime(2026, 3, 1, 10, 0, tzinfo=UTC))
        self.assertLess(a, b)

    def test_a_naive_datetime_is_refused(self):
        with self.assertRaises(ValueError):
            keys.iso(dt.datetime(2026, 3, 1, 9, 0))
