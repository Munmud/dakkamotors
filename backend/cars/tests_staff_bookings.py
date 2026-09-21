"""The staff test-drive queue and the slot list.

These replace `TestDriveBookingAdmin` and `TestDriveSlotAdmin`, and they carry the
assertions that used to live in `AdminStatusChangeTests`. That class protected a
property learned the hard way: `list_editable` and the default `save_model` both moved a
booking's status without going near `confirm_booking`, so the row said confirmed and the
customer was never told.

There is no status widget on these pages at all -- only buttons that call the domain
functions by name -- so the property is structural now rather than defended. Asserted
anyway, because "structural" is a claim about code that can be edited.
"""

import datetime as dt
import json
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .choices import BookingStatus, NotificationKind
from .store import slots as slot_store
from .tests import (
    DynamoReset, MAIL_SETTINGS, bell, future_slot, make_booking, make_customer,
)
from .tests_fake_cognito import FakeCognito, sign_in, sign_out
from .tests_staff import make_staff


@override_settings(**MAIL_SETTINGS)
class StaffBookingQueueTests(FakeCognito, DynamoReset, SimpleTestCase):
    """Soonest first, with the phone number on it.

    Until the confirmation emails existed this page was the only way anyone found out a
    customer was coming, so it still leads with when, who and how to reach them.
    """

    def setUp(self):
        super().setUp()
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)
        self.customer, _ = make_customer("buyer@example.com")
        self.url = reverse("staff:booking-list")

    def test_the_queue_shows_when_who_and_how_to_reach_them(self):
        make_booking(self.customer, future_slot(), car_label="Tanto")

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "buyer@example.com")
        self.assertContains(response, "080-1111-2222")
        self.assertContains(response, "Tanto")

    def test_the_queue_is_ordered_by_appointment_time(self):
        later, _ = make_customer("later@example.com")
        make_booking(later, future_slot(days=9))
        make_booking(self.customer, future_slot(days=2))

        body = self.client.get(self.url).content.decode("utf-8")

        self.assertLess(body.index("buyer@example.com"),
                        body.index("later@example.com"))

    def test_the_queue_counts_what_is_awaiting_confirmation(self):
        make_booking(self.customer, future_slot(days=2))
        make_booking(self.customer, future_slot(days=3))

        self.assertContains(self.client.get(self.url), "2 awaiting confirmation")

    def test_searching_narrows_by_email(self):
        other, _ = make_customer("someone.else@example.com")
        make_booking(self.customer, future_slot(days=2))
        make_booking(other, future_slot(days=3))

        response = self.client.get(self.url, {"q": "someone.else"})

        self.assertContains(response, "someone.else@example.com")
        self.assertNotContains(response, "buyer@example.com")

    def test_a_guest_is_sent_to_sign_in(self):
        sign_out(self.client, staff=True)
        self.assertEqual(self.client.get(self.url).status_code, 302)


@override_settings(**MAIL_SETTINGS)
class StaffBookingActionTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)
        self.customer, _ = make_customer("buyer@example.com")
        self.booking = make_booking(self.customer, future_slot(), car_label="Tanto")
        self.url = reverse("staff:booking-detail", args=[self.booking.booking_id])

    def act(self, action):
        with mock.patch("cars.mail.boto3.client") as client:
            response = self.client.post(self.url, {"action": action}, follow=True)
            calls = client.return_value.put_object.call_args_list
        sent = [json.loads(c.kwargs["Body"].decode("utf-8")) for c in calls]
        return response, sent

    def test_confirming_emails_the_customer(self):
        response, sent = self.act("confirm")

        self.booking.refresh()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)
        self.assertIsNotNone(self.booking.confirmed_at)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["to"], ["buyer@example.com"])
        self.assertIn("confirmed", sent[0]["subject"].lower())
        self.assertContains(response, "has been emailed")

    def test_confirming_reaches_the_bell(self):
        self.act("confirm")
        self.assertEqual(
            len(bell(self.customer, NotificationKind.BOOKING_CONFIRMED)), 1)

    def test_confirming_twice_tells_them_once(self):
        self.act("confirm")
        response, sent = self.act("confirm")

        self.assertEqual(sent, [])
        self.assertContains(response, "already confirmed")
        self.assertEqual(len(bell(self.customer)), 1)

    def test_cancelling_tells_the_customer(self):
        response, sent = self.act("cancel")

        self.booking.refresh()
        self.assertEqual(self.booking.status, BookingStatus.CANCELLED)
        self.assertEqual(len(sent), 1)
        self.assertIn("cancelled", sent[0]["subject"].lower())
        self.assertContains(response, "customer has been told")

    def test_cancelling_releases_the_seat(self):
        slot_id = self.booking.slot_id
        self.assertEqual(slot_store.get(slot_id).booked_count, 1)

        self.act("cancel")

        self.assertEqual(slot_store.get(slot_id).booked_count, 0)
        self.assertEqual(slot_store.roster(slot_id), [])

    def test_marking_attendance_emails_nobody(self):
        """The visit has already happened; a message about it would be noise."""
        response, sent = self.act("complete")

        self.booking.refresh()
        self.assertEqual(self.booking.status, BookingStatus.COMPLETED)
        self.assertEqual(sent, [])
        self.assertEqual(len(bell(self.customer)), 0)
        self.assertContains(response, "Marked as attended")

    def test_a_no_show_also_releases_the_seat(self):
        slot_id = self.booking.slot_id

        self.act("no_show")

        self.booking.refresh()
        self.assertEqual(self.booking.status, BookingStatus.NO_SHOW)
        self.assertEqual(slot_store.get(slot_id).booked_count, 0)

    def test_an_unknown_action_changes_nothing(self):
        response, sent = self.act("explode")

        self.booking.refresh()
        self.assertEqual(self.booking.status, BookingStatus.PENDING)
        self.assertEqual(sent, [])
        self.assertContains(response, "did not do anything")

    def test_the_detail_page_shows_everyone_in_the_slot(self):
        other, _ = make_customer("second@example.com")
        make_booking(other, slot_store.get(self.booking.slot_id))

        response = self.client.get(self.url)

        self.assertContains(response, "buyer@example.com")
        self.assertContains(response, "second@example.com")


@override_settings(**MAIL_SETTINGS)
class StaffSlotTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)
        self.url = reverse("staff:slot-list")

    def test_slots_are_listed_with_their_occupancy(self):
        slot = future_slot(days=2, capacity=2)
        customer, _ = make_customer("buyer@example.com")
        make_booking(customer, slot)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1 / 2")

    def test_closing_a_slot_keeps_the_bookings_already_taken(self):
        """The wording the old admin action used, and still the right behaviour.

        Staff close a slot to stop *new* bookings. Cancelling the existing ones is a
        separate, deliberate act, because each one emails a person.
        """
        slot = future_slot(days=2)
        customer, _ = make_customer("buyer@example.com")
        make_booking(customer, slot)

        response = self.client.post(
            reverse("staff:slot-toggle", args=[slot.slot_id]), follow=True)

        self.assertFalse(slot_store.get(slot.slot_id).is_open)
        self.assertEqual(slot_store.get(slot.slot_id).booked_count, 1)
        self.assertContains(response, "still has their appointment")

    def test_a_closed_slot_can_be_reopened(self):
        slot = future_slot(days=2, is_open=False)

        self.client.post(reverse("staff:slot-toggle", args=[slot.slot_id]), follow=True)

        self.assertTrue(slot_store.get(slot.slot_id).is_open)

    def test_the_date_range_narrows_the_list(self):
        future_slot(days=2, hour=9)
        future_slot(days=40, hour=9)

        today = timezone.localdate()
        response = self.client.get(self.url, {
            "start": today.isoformat(),
            "end": (today + dt.timedelta(days=5)).isoformat(),
        })

        self.assertEqual(len(response.context["slots"]), 1)

    def test_a_guest_is_sent_to_sign_in(self):
        sign_out(self.client, staff=True)
        self.assertEqual(self.client.get(self.url).status_code, 302)
