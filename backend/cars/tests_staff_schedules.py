"""The weekly availability rules. Replaces `TestDriveScheduleAdmin`.

The rules themselves are simple; what is worth asserting is what they do *not* disturb.
Editing one must not shrink an evening somebody has already booked, and deleting one
must not take anyone's appointment with it.
"""

import datetime as dt

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .store import schedules as schedule_store
from .store import slots as slot_store
from .store.models import RuleGuard
from .store import keys
from .tests import (
    DynamoReset, MAIL_SETTINGS, make_booking, make_customer, make_schedule,
)
from .tests_staff import make_staff, make_staff_without_permissions


def rule_fields(**overrides):
    fields = {
        "weekday": "4",
        "start_time": "18:30",
        "end_time": "19:00",
        "capacity": "2",
        "is_active": "on",
        "starts_on": "",
        "ends_on": "",
        "note": "",
    }
    fields.update(overrides)
    return fields


@override_settings(**MAIL_SETTINGS)
class StaffScheduleTests(DynamoReset, TestCase):
    def setUp(self):
        super().setUp()
        self.staff, _ = make_staff()
        self.client.force_login(self.staff)
        self.url = reverse("staff:schedule-list")

    def test_a_guest_is_sent_to_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_staff_without_the_permission_are_refused(self):
        self.client.force_login(make_staff_without_permissions())
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_an_empty_list_says_no_times_are_on_offer(self):
        self.assertContains(self.client.get(self.url), "no test drive times are on offer")

    def test_adding_a_rule_lists_it(self):
        response = self.client.post(reverse("staff:schedule-add"), rule_fields(),
                                    follow=True)

        self.assertContains(response, "Friday")
        self.assertContains(response, "18:30")
        self.assertEqual(len(schedule_store.all_rules()), 1)

    def test_a_duplicate_weekday_and_time_is_refused(self):
        self.client.post(reverse("staff:schedule-add"), rule_fields(), follow=True)

        response = self.client.post(reverse("staff:schedule-add"), rule_fields(),
                                    follow=True)

        self.assertContains(response, "already a rule for that weekday and time")
        self.assertEqual(len(schedule_store.all_rules()), 1)

    def test_an_end_date_before_the_start_date_is_refused(self):
        today = timezone.localdate()
        response = self.client.post(
            reverse("staff:schedule-add"),
            rule_fields(starts_on=today.isoformat(),
                        ends_on=(today - dt.timedelta(days=1)).isoformat()),
            follow=True)

        self.assertContains(response, "cannot be before the start date")
        self.assertEqual(schedule_store.all_rules(), [])

    def test_rules_are_listed_weekday_then_time(self):
        make_schedule(weekday=4, start="18:30", end="19:00")
        make_schedule(weekday=1, start="10:00", end="10:30")

        body = self.client.get(self.url).content.decode("utf-8")

        self.assertLess(body.index("Tuesday"), body.index("Friday"))

    def test_pausing_a_rule_stops_new_slots_being_generated(self):
        rule = make_schedule()

        self.client.post(reverse("staff:schedule-edit", args=[rule.schedule_id]),
                         rule_fields(weekday=str(rule.weekday),
                                     start_time=rule.start_time.strftime("%H:%M"),
                                     end_time=rule.end_time.strftime("%H:%M"),
                                     is_active=""),
                         follow=True)

        self.assertFalse(schedule_store.get(rule.schedule_id).is_active)
        self.assertEqual(schedule_store.active(), [])

    def test_changing_the_time_moves_the_uniqueness_guard(self):
        rule = make_schedule(weekday=4, start="18:30", end="19:00")

        self.client.post(reverse("staff:schedule-edit", args=[rule.schedule_id]),
                         rule_fields(weekday="4", start_time="19:30",
                                     end_time="20:00"),
                         follow=True)

        self.assertEqual(
            RuleGuard.get(keys.rule_guard_pk(4, dt.time(19, 30), dt.time(20, 0)),
                          keys.GUARD).schedule_id,
            rule.schedule_id)
        with self.assertRaises(RuleGuard.DoesNotExist):
            RuleGuard.get(keys.rule_guard_pk(4, dt.time(18, 30), dt.time(19, 0)),
                          keys.GUARD)

    def test_editing_a_rule_does_not_shrink_an_evening_already_booked(self):
        """A slot's capacity is a snapshot, not a live lookup."""
        from . import booking as rules

        rule = make_schedule(capacity=2)
        rules.ensure_slots(horizon_days=14)
        before = [s.capacity for s in slot_store.between(
            timezone.now(), timezone.now() + dt.timedelta(days=14))]
        self.assertTrue(before and all(c == 2 for c in before))

        self.client.post(reverse("staff:schedule-edit", args=[rule.schedule_id]),
                         rule_fields(weekday=str(rule.weekday),
                                     start_time=rule.start_time.strftime("%H:%M"),
                                     end_time=rule.end_time.strftime("%H:%M"),
                                     capacity="1"),
                         follow=True)
        rules.ensure_slots(horizon_days=14)

        after = [s.capacity for s in slot_store.between(
            timezone.now(), timezone.now() + dt.timedelta(days=14))]
        self.assertTrue(all(c == 2 for c in after))

    def test_deleting_a_rule_keeps_the_appointments_it_produced(self):
        """`on_delete=SET_NULL` was doing this. There is no cascade now, so the sweep
        is explicit -- and it must leave the booking alone."""
        from . import booking as rules

        rule = make_schedule(capacity=2)
        rules.ensure_slots(horizon_days=14)
        slot = slot_store.between(timezone.now(),
                                  timezone.now() + dt.timedelta(days=14))[0]
        customer, _ = make_customer("buyer@example.com")
        booking = make_booking(customer, slot)

        response = self.client.post(
            reverse("staff:schedule-edit", args=[rule.schedule_id]),
            {"action": "delete"}, follow=True)

        self.assertContains(response, "nobody loses an appointment")
        self.assertIsNone(schedule_store.find(rule.schedule_id))
        # The slot and the booking both survive; the slot just no longer claims a rule.
        surviving = slot_store.get(slot.slot_id)
        self.assertEqual(surviving.booked_count, 1)
        self.assertIsNone(surviving.schedule_id)
        booking.refresh()
        self.assertEqual(booking.slot_id, slot.slot_id)

    def test_deleting_a_rule_frees_its_weekday_and_time(self):
        rule = make_schedule(weekday=4, start="18:30", end="19:00")

        self.client.post(reverse("staff:schedule-edit", args=[rule.schedule_id]),
                         {"action": "delete"}, follow=True)

        # The same slot in the week can be described again.
        make_schedule(weekday=4, start="18:30", end="19:00")
        self.assertEqual(len(schedule_store.all_rules()), 1)

    def test_a_missing_rule_is_a_404(self):
        self.assertEqual(
            self.client.get(reverse("staff:schedule-edit", args=["nope"])).status_code,
            404)
