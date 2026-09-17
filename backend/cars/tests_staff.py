"""The server-rendered staff pages.

These replace `CarQuestionAdmin`, so they inherit its responsibilities: answering must
email the customer, publishing must be impossible without an answer, and neither may be
reachable by someone who is not staff. The admin gave all three for free; here they are
code, so they are tested.
"""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .store import questions as question_store
from .tests import (
    DynamoReset, MAIL_SETTINGS, make_car, make_customer, make_manager, make_question,
)


def make_staff(username="staffer"):
    """An Inventory Manager, which is what the shop's staff actually are.

    Reuses the same group the deploy reconciles, so these tests exercise the real
    permission set rather than an invented one - and `@requires` in staff/auth.py maps
    onto exactly those permissions while Django still holds them.
    """
    return make_manager(username=username)


def make_staff_without_permissions(username="newstarter"):
    """is_staff, but in no group. The admin would show them an empty index."""
    user = get_user_model().objects.create_user(username=username,
                                                password="staff-pw-123456")
    user.is_staff = True
    user.save()
    return user


@override_settings(**MAIL_SETTINGS)
class StaffQuestionAccessTests(DynamoReset, TestCase):
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
        self.assertIn("/api/admin/login/", response["Location"])

    def test_a_signed_in_customer_is_not_staff(self):
        self.client.force_login(self.customer)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_a_staff_member_sees_the_queue(self):
        staff, _ = make_staff()
        self.client.force_login(staff)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Any service history?")

    def test_staff_without_the_permission_are_refused(self):
        """`@requires` is a real guard, not decoration.

        Being staff gets you through the door; the group decides what you may read.
        A new starter in no group sees nothing, exactly as the admin index would be
        empty for them.
        """
        self.client.force_login(make_staff_without_permissions())

        self.assertEqual(self.client.get(self.url).status_code, 403)


@override_settings(**MAIL_SETTINGS)
class StaffQuestionQueueTests(DynamoReset, TestCase):
    def setUp(self):
        super().setUp()
        self.car = make_car("STAFF-2", brand="Daihatsu", model_name="Tanto")
        self.other = make_car("STAFF-3", brand="Suzuki", model_name="Alto")
        self.customer, _ = make_customer("asker@example.com")
        self.staff, _ = make_staff()
        self.client.force_login(self.staff)
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
class StaffAnsweringTests(DynamoReset, TestCase):
    """The page must not be able to skip what the domain layer promises."""

    def setUp(self):
        super().setUp()
        self.car = make_car("STAFF-4", brand="Honda", model_name="N-Box")
        self.customer, _ = make_customer("asker@example.com")
        self.staff, _ = make_staff()
        self.client.force_login(self.staff)
        self.question = make_question(self.car, customer=self.customer,
                                      question="Any service history?")
        self.url = reverse("staff:question-detail", args=[self.question.question_id])

    def post(self, **data):
        with mock.patch("cars.mail.boto3.client") as client:
            with self.captureOnCommitCallbacks(execute=True):
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
        self.assertContains(response, "Unknown action")


@override_settings(**MAIL_SETTINGS)
class StaffPublishingTests(DynamoReset, TestCase):
    def setUp(self):
        super().setUp()
        self.car = make_car("STAFF-5", brand="Honda", model_name="N-Box")
        self.customer, _ = make_customer("asker@example.com")
        self.staff, _ = make_staff()
        self.client.force_login(self.staff)

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
            [q.question_id for q in question_store.published_for(str(self.car.pk))],
            [question.question_id],
        )

    def test_unpublishing_takes_it_off_the_page(self):
        question = make_question(self.car, customer=self.customer, question="Colour?",
                                 answer="Pearl white.", published=True)

        self.client.post(self.detail_url(question), {"action": "unpublish"}, follow=True)

        question.refresh()
        self.assertFalse(question.is_published)
        self.assertEqual(question_store.published_for(str(self.car.pk)), [])

    def test_publishing_moves_the_cars_updated_at_so_the_sitemap_notices(self):
        question = make_question(self.car, customer=self.customer, question="Colour?",
                                 answer="Pearl white.", answered=True)
        self.car.refresh_from_db()
        before = self.car.updated_at

        self.client.post(self.detail_url(question), {"action": "publish"}, follow=True)

        self.car.refresh_from_db()
        self.assertGreater(self.car.updated_at, before)

    def test_the_publish_button_is_disabled_until_there_is_an_answer(self):
        """Courtesy, not the guard - but staff should not be offered a button that
        refuses them."""
        question = make_question(self.car, customer=self.customer, question="Colour?")

        response = self.client.get(self.detail_url(question))

        self.assertContains(response, "disabled")
        self.assertContains(response, "Write an answer first")
