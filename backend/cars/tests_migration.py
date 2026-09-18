"""What survives of the migration tests once the ORM has gone.

`booked_count` and `active_bookings` are derived state the application now owns, and the
seat items are the source of truth for them. Reconciliation is the answer to a restore,
to a partial import, and to any doubt about a counter -- so it outlives the cutover that
introduced it, and it is tested here.

The export/import round trip that used to live here reads Django models and therefore
only runs at the `pre-dynamo` tag. See the note below.
"""

import datetime as dt
import io
import json
import tempfile

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from .store import bookings as booking_store
from .store import customers as customer_store
from .store import slots as slot_store
from .tests import DynamoReset, MAIL_SETTINGS
from .tests_fake_cognito import FakeCognito


# `MigrationRoundTripTests` lived here and is gone with the ORM it read.
#
# It exported from Django models into DynamoDB and compared the two sides, so it cannot
# run against a tree that has no models -- and it must not be rewritten to fake them,
# because the whole value of it was that the source was the real thing. It still runs,
# at the `pre-dynamo` tag, which is also the only commit `export_aurora` will run from:
#
#     git checkout pre-dynamo
#     python manage.py test cars.tests_migration
#
# That is the rehearsal the cutover depends on, and `docs/INFRA.md` says to repeat it
# until it is boring. What stays here is the half that is store-only and still has
# something to act on after the window: the counter reconciliation.


@override_settings(**MAIL_SETTINGS)
class ReconcileCountersTests(FakeCognito, DynamoReset, SimpleTestCase):
    """The repair for the two counters that replaced COUNT(*) under a lock."""

    def setUp(self):
        super().setUp()
        from .tests import future_slot, make_booking, make_customer

        self.customer, _ = make_customer("buyer@example.com")
        self.slot = future_slot(capacity=2)
        self.booking = make_booking(self.customer, self.slot)

    def test_a_correct_table_reports_no_drift(self):
        out = io.StringIO()
        call_command("reconcile_counters", stdout=out)
        self.assertIn("Every counter agrees", out.getvalue())

    def test_drift_is_reported_without_being_written(self):
        from .store.models import Slot

        slot = slot_store.get(self.slot.slot_id)
        slot.update(actions=[Slot.booked_count.set(7)])

        out = io.StringIO()
        call_command("reconcile_counters", stdout=out)

        self.assertIn("7 -> 1", out.getvalue())
        self.assertEqual(slot_store.get(self.slot.slot_id).booked_count, 7)

    def test_fix_repairs_both_counters_from_their_items(self):
        from .store.models import Customer, Slot

        slot_store.get(self.slot.slot_id).update(
            actions=[Slot.booked_count.set(7)])
        customer_store.get(str(self.customer.pk)).update(
            actions=[Customer.active_bookings.set(9)])

        call_command("reconcile_counters", fix=True, stdout=io.StringIO())

        self.assertEqual(slot_store.get(self.slot.slot_id).booked_count, 1)
        self.assertEqual(
            customer_store.get(str(self.customer.pk)).active_bookings, 1)
