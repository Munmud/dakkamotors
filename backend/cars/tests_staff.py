"""The server-rendered staff pages.

These replace `CarQuestionAdmin`, so they inherit its responsibilities: answering must
email the customer, publishing must be impossible without an answer, and neither may be
reachable by someone who is not staff. The admin gave all three for free; here they are
code, so they are tested.
"""

import json
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from .store import cars as car_store
from .store import questions as question_store
from .store import requests as request_store
from .tests import (
    DynamoReset, MAIL_SETTINGS, future_slot, make_booking, make_car, make_customer,
    make_manager, make_question,
)
from . import cognito
from . import tests_fake_cognito as fake_cognito
from .tests_fake_cognito import FakeCognito, sign_in


def make_staff(username="staffer"):
    """An inventory manager, which is what the shop's staff actually are.

    The groups are the real ones `permissions.py` reads, so these tests exercise the
    actual permission set rather than an invented one.
    """
    return make_manager(username=username)


def make_owner(username="owner"):
    """An owner: everything, including what OWNER_ONLY holds back from a manager."""
    user = fake_cognito.make_user(f"{username}@example.com", name="The Owner",
                                  groups=(cognito.STAFF_GROUP, cognito.OWNERS_GROUP))
    return user, "owner-pw-123456"


def make_staff_without_permissions(username="newstarter"):
    """In `staff`, and in no other group.

    The door and the rooms are separate: `staff` is what gets somebody past
    `staff_required`, and a group is what decides which pages they may then read. This
    is a new starter nobody has given a role to yet.
    """
    return fake_cognito.make_user(f"{username}@example.com", name="New Starter",
                                  groups=(cognito.STAFF_GROUP,))


@override_settings(**MAIL_SETTINGS)
class StaffQuestionAccessTests(FakeCognito, DynamoReset, SimpleTestCase):
    """Who may reach the queue at all."""

    def setUp(self):
        super().setUp()
        self.car = make_car("STAFF-1", brand="Honda", model_name="N-Box")
        self.customer, _ = make_customer("asker@example.com")
        self.question = make_question(self.car, customer=self.customer,
                                      question="Any service history?")
        self.url = reverse("staff:question-list")

    def test_a_guest_is_sent_to_sign_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/api/staff/auth/not-configured", response["Location"])

    def test_a_signed_in_customer_is_not_staff(self):
        sign_in(self.client, self.customer)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_a_staff_member_sees_the_queue(self):
        staff, _ = make_staff()
        sign_in(self.client, staff, staff=True)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Any service history?")

    def test_staff_without_the_permission_are_refused(self):
        """`@requires` is a real guard, not decoration.

        Being staff gets you through the door; the group decides what you may read.
        A new starter in no group sees nothing, exactly as the admin index would be
        empty for them.
        """
        sign_in(self.client, make_staff_without_permissions(), staff=True)

        self.assertEqual(self.client.get(self.url).status_code, 403)


@override_settings(**MAIL_SETTINGS)
class StaffQuestionQueueTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.car = make_car("STAFF-2", brand="Daihatsu", model_name="Tanto")
        self.other = make_car("STAFF-3", brand="Suzuki", model_name="Alto")
        self.customer, _ = make_customer("asker@example.com")
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)
        self.url = reverse("staff:question-list")

    def test_searching_narrows_the_queue(self):
        make_question(self.car, customer=self.customer, question="Does it have a tow bar?")
        make_question(self.car, customer=self.customer, question="Is the aircon cold?")

        response = self.client.get(self.url, {"q": "tow bar"})

        self.assertContains(response, "tow bar")
        self.assertNotContains(response, "aircon")

    def test_filtering_by_brand_narrows_the_queue(self):
        make_question(self.car, customer=self.customer, question="About the Tanto")
        make_question(self.other, customer=self.customer, question="About the Alto")

        response = self.client.get(self.url, {"brand": "Suzuki"})

        self.assertContains(response, "About the Alto")
        self.assertNotContains(response, "About the Tanto")

    def test_filtering_by_state_separates_the_backlog_from_the_done(self):
        make_question(self.car, customer=self.customer, question="Still waiting")
        make_question(self.car, customer=self.customer, question="Already public",
                      answer="Yes.", published=True)

        waiting = self.client.get(self.url, {"state": "unanswered"})
        self.assertContains(waiting, "Still waiting")
        self.assertNotContains(waiting, "Already public")

        public = self.client.get(self.url, {"state": "published"})
        self.assertContains(public, "Already public")
        self.assertNotContains(public, "Still waiting")

    def test_the_queue_counts_what_is_waiting(self):
        make_question(self.car, customer=self.customer, question="One")
        make_question(self.car, customer=self.customer, question="Two")
        make_question(self.car, customer=self.customer, question="Done",
                      answer="Yes.", answered=True)

        self.assertContains(self.client.get(self.url), "2 waiting")


@override_settings(**MAIL_SETTINGS)
class StaffAnsweringTests(FakeCognito, DynamoReset, SimpleTestCase):
    """The page must not be able to skip what the domain layer promises."""

    def setUp(self):
        super().setUp()
        self.car = make_car("STAFF-4", brand="Honda", model_name="N-Box")
        self.customer, _ = make_customer("asker@example.com")
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)
        self.question = make_question(self.car, customer=self.customer,
                                      question="Any service history?")
        self.url = reverse("staff:question-detail", args=[self.question.question_id])

    def post(self, **data):
        with mock.patch("cars.mail.boto3.client") as client:
            response = self.client.post(self.url, data, follow=True)
            calls = client.return_value.put_object.call_args_list
        sent = [json.loads(c.kwargs["Body"].decode("utf-8")) for c in calls]
        return response, sent

    def test_answering_saves_and_emails_the_customer(self):
        response, sent = self.post(action="answer", answer="Full history, stamped.")

        self.assertEqual(response.status_code, 200)
        self.question.refresh()
        self.assertEqual(self.question.answer, "Full history, stamped.")
        self.assertIsNotNone(self.question.answered_at)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["to"], ["asker@example.com"])
        self.assertContains(response, "emailed to the customer")

    def test_correcting_an_answer_does_not_email_again(self):
        self.post(action="answer", answer="Full histroy.")

        response, sent = self.post(action="answer", answer="Full history.")

        self.assertEqual(sent, [])
        self.assertContains(response, "not emailed again")
        self.question.refresh()
        self.assertEqual(self.question.answer, "Full history.")

    def test_clearing_an_answer_says_so_and_does_not_mark_it_answered(self):
        response, sent = self.post(action="answer", answer="   ")

        self.assertEqual(sent, [])
        self.question.refresh()
        self.assertIsNone(self.question.answered_at)
        self.assertContains(response, "has not been told")

    def test_the_question_text_can_be_tidied_before_publishing(self):
        """People type their phone number into free text, and this goes on a page."""
        self.post(action="edit-question", question="Does it have service history?")

        self.question.refresh()
        self.assertEqual(self.question.question, "Does it have service history?")

    def test_an_unknown_action_changes_nothing(self):
        response, sent = self.post(action="explode", answer="Nope.")

        self.assertEqual(sent, [])
        self.question.refresh()
        self.assertEqual(self.question.answer, "")
        self.assertContains(response, "did not do anything")


@override_settings(**MAIL_SETTINGS)
class StaffPublishingTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.car = make_car("STAFF-5", brand="Honda", model_name="N-Box")
        self.customer, _ = make_customer("asker@example.com")
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)

    def detail_url(self, question):
        return reverse("staff:question-detail", args=[question.question_id])

    def test_publishing_without_an_answer_is_refused_with_a_reason(self):
        """The page's own guard, since the CheckConstraint that used to catch this is
        gone. The store's ConditionExpression is the backstop underneath it."""
        question = make_question(self.car, customer=self.customer, question="Colour?")

        response = self.client.post(self.detail_url(question),
                                    {"action": "publish"}, follow=True)

        question.refresh()
        self.assertFalse(question.is_published)
        self.assertContains(response, "Write an answer before publishing")

    def test_publishing_an_answered_question_puts_it_on_the_page(self):
        question = make_question(self.car, customer=self.customer, question="Colour?",
                                 answer="Pearl white.", answered=True)

        response = self.client.post(self.detail_url(question),
                                    {"action": "publish"}, follow=True)

        question.refresh()
        self.assertTrue(question.is_published)
        self.assertContains(response, "allow up to five minutes")
        self.assertEqual(
            [q.question_id for q in question_store.published_for(self.car.car_id)],
            [question.question_id],
        )

    def test_unpublishing_takes_it_off_the_page(self):
        question = make_question(self.car, customer=self.customer, question="Colour?",
                                 answer="Pearl white.", published=True)

        self.client.post(self.detail_url(question), {"action": "unpublish"}, follow=True)

        question.refresh()
        self.assertFalse(question.is_published)
        self.assertEqual(question_store.published_for(self.car.car_id), [])

    def test_publishing_moves_the_cars_updated_at_so_the_sitemap_notices(self):
        question = make_question(self.car, customer=self.customer, question="Colour?",
                                 answer="Pearl white.", answered=True)
        before = car_store.get(self.car.car_id).updated_at

        self.client.post(self.detail_url(question), {"action": "publish"}, follow=True)

        self.assertGreater(car_store.get(self.car.car_id).updated_at, before)

    def test_the_publish_button_is_disabled_until_there_is_an_answer(self):
        """Courtesy, not the guard - but staff should not be offered a button that
        refuses them."""
        question = make_question(self.car, customer=self.customer, question="Colour?")

        response = self.client.get(self.detail_url(question))

        self.assertContains(response, "disabled")
        self.assertContains(response, "Write an answer first")


class MastheadCountTests(FakeCognito, DynamoReset, SimpleTestCase):
    """The number on the tab is the same number the page shows, on every page."""

    def setUp(self):
        super().setUp()
        self.car = make_car("COUNT-1")
        self.customer, _ = make_customer("buyer@example.com")
        self.staff, _ = make_manager()
        sign_in(self.client, self.staff, staff=True)

    def nav(self, route="staff:car-list"):
        html = self.client.get(reverse(route)).content.decode()
        return html.split("<nav>")[1].split("</nav>")[0]

    def test_nothing_waiting_means_no_numbers(self):
        self.assertNotIn("nav__count", self.nav())

    def test_each_tab_counts_its_own_backlog_from_any_page(self):
        make_question(self.car, customer=self.customer, question="Unanswered one")
        make_question(self.car, customer=self.customer, question="Unanswered two")
        make_question(self.car, customer=self.customer, question="Answered",
                      answer="Yes", answered=True)
        make_booking(self.customer, future_slot(), car=self.car)
        request_store.create(name="Hana", email="h@example.com", phone="1",
                             details="A Tanto")

        for route in ("staff:car-list", "staff:schedule-list"):
            nav = self.nav(route)
            self.assertIn('Questions <span class="nav__count">2', nav)
            self.assertIn('Test drives <span class="nav__count">1', nav)
            self.assertIn('Requests <span class="nav__count">1', nav)

    def test_dealing_with_it_takes_the_number_off_the_tab(self):
        wish = request_store.create(name="Hana", email="h@example.com", phone="1",
                                    details="A Tanto")
        self.assertIn('Requests <span class="nav__count">1', self.nav())

        request_store.resolve(wish, staff_sub="x")

        self.assertNotIn("nav__count", self.nav())
