"""Tests for store.questions, store.notifications, store.customers and store.search."""

import datetime as dt
import pathlib
import re

from django.test import SimpleTestCase

from .choices import NotificationKind, QuestionLanguage
from .store import cars, customers, keys, notifications, questions, search
from .store.errors import ConditionFailed
from .store.models import CarQuestion, DedupeGuard, Notification
from .tests_store import DynamoTestCase
from .tests_store_cars import make_car

UTC = dt.timezone.utc


class QuestionStoreTests(DynamoTestCase):
    def setUp(self):
        super().setUp()
        cars.clear_slug_cache()
        self.now = dt.datetime.now(UTC)
        self.car = cars.create(make_car("1"), now=self.now)

    def ask(self, text="Is it accident free?", sub="alice", car=None):
        car = car or self.car
        return questions.create(
            car_id=car.car_id, car_brand=car.brand, car_model_name=car.model_name,
            car_slug=car.slug, car_label=str(car),
            customer_sub=sub, customer_email=f"{sub}@example.com",
            question=text, now=self.now)

    def test_asking_denormalises_the_car_for_the_staff_queue(self):
        q = self.ask()
        self.assertEqual(q.car_brand, "Daihatsu")
        self.assertEqual(q.car_slug, self.car.slug)
        self.assertFalse(q.is_published)
        self.assertFalse(q.is_answered)
        self.assertEqual(q.state, "Needs an answer")

    def test_an_unanswered_question_is_not_published(self):
        q = self.ask()
        with self.assertRaises(ConditionFailed):
            questions.publish(q, now=self.now)
        self.assertFalse(questions.get("1", q.sk).is_published)

    def test_answering_then_publishing_shows_it_on_the_car(self):
        q = self.ask()
        q, first = questions.record_answer(question=q, answer="Yes, clean history.",
                                           staff_sub="staff-1", now=self.now)
        self.assertTrue(first)
        self.assertEqual(q.state, "Answered, not public")
        self.assertEqual(questions.published_for("1"), [])

        questions.publish(q, now=self.now)
        published = questions.published_for("1")
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].state, "Published")

    def test_only_the_first_answer_reports_itself_as_the_first(self):
        """What replaces `select_for_update()`: exactly one caller sends the email."""
        q = self.ask()
        _, first = questions.record_answer(question=q, answer="Yes.",
                                           staff_sub="s1", now=self.now)
        self.assertTrue(first)
        q2 = questions.get("1", q.sk)
        _, again = questions.record_answer(question=q2, answer="Yes, definitely.",
                                           staff_sub="s1",
                                           now=self.now + dt.timedelta(days=30))
        self.assertFalse(again, "fixing a typo must not re-notify the customer")
        self.assertEqual(questions.get("1", q.sk).answer, "Yes, definitely.")

    def test_answered_at_is_not_moved_by_a_later_edit(self):
        q = self.ask()
        q, _ = questions.record_answer(question=q, answer="Yes.", staff_sub="s1",
                                       now=self.now)
        stamped = questions.get("1", q.sk).answered_at
        later = self.now + dt.timedelta(days=30)
        questions.record_answer(question=questions.get("1", q.sk), answer="Still yes.",
                                staff_sub="s1", now=later)
        self.assertEqual(questions.get("1", q.sk).answered_at, stamped)

    def test_a_blank_answer_never_marks_the_question_answered(self):
        """Otherwise the customer would silently never hear back."""
        q = self.ask()
        q, first = questions.record_answer(question=q, answer="   ", staff_sub="s1",
                                           now=self.now)
        self.assertFalse(first)
        self.assertIsNone(questions.get("1", q.sk).answered_at)
        self.assertFalse(questions.get("1", q.sk).is_answered)

    def test_publishing_moves_the_cars_updated_at_so_the_sitemap_notices(self):
        q = self.ask()
        q, _ = questions.record_answer(question=q, answer="Yes.", staff_sub="s1",
                                       now=self.now)
        later = self.now + dt.timedelta(hours=2)
        questions.publish(q, now=later)
        self.assertEqual(cars.get("1").updated_at, later)

    def test_unpublishing_takes_it_off_the_public_page(self):
        q = self.ask()
        q, _ = questions.record_answer(question=q, answer="Yes.", staff_sub="s1",
                                       now=self.now)
        questions.publish(q, now=self.now)
        questions.unpublish(questions.get("1", q.sk), now=self.now)
        self.assertEqual(questions.published_for("1"), [])

    def test_a_customer_sees_their_own_questions_across_cars(self):
        cars.create(make_car("2", grade="Z"), now=self.now)
        self.ask(sub="alice")
        self.ask(sub="alice", text="Second car?", car=cars.get("2"))
        self.ask(sub="bob")
        self.assertEqual(len(questions.for_customer("alice")), 2)
        self.assertEqual(questions.open_count("alice"), 2)

    def test_editing_the_question_text_updates_the_search_blob(self):
        q = self.ask(text="my number is 080-1111-2222")
        questions.edit_text(q, text="Does it have a tow bar?", now=self.now)
        refreshed = questions.get("1", q.sk)
        self.assertEqual(refreshed.question, "Does it have a tow bar?")
        self.assertNotIn("080-1111-2222", refreshed.search_blob)


class PublishedFlagIsWrittenInOnePlaceTests(SimpleTestCase):
    """The compensation for losing `published_question_has_an_answer`.

    A CheckConstraint guarded the table against every writer. A ConditionExpression
    guards only the write it is attached to. That reduction is acceptable *only* while
    `store/questions.py` is the sole module that writes the attribute, so this asserts
    it mechanically rather than trusting a convention.
    """

    def test_no_other_store_module_writes_is_published(self):
        store_dir = pathlib.Path(__file__).parent / "store"
        # Only actual writes. `is_published=None` in a function signature is a read
        # filter, and `is_published = BooleanAttribute(...)` in models.py is the field
        # declaration -- neither sets the flag on a stored item.
        writers = (
            re.compile(r"is_published\.set\("),          # a PynamoDB update action
            re.compile(r"\.is_published\s*=(?!=)"),       # attribute assignment
            re.compile(r"is_published\s*=\s*(True|False)"),  # constructor keyword
        )
        offenders = []
        for path in store_dir.glob("*.py"):
            if path.name == "questions.py":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if "BooleanAttribute" in line:
                    continue
                if any(w.search(line) for w in writers):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "is_published must only ever be written by store/questions.py -- the "
            "ConditionExpression there is all that is left of the CheckConstraint.",
        )


class NotificationStoreTests(DynamoTestCase):
    def setUp(self):
        super().setUp()
        self.now = dt.datetime.now(UTC)

    def notify(self, dedupe="booking:1:confirmed", sub="alice", when=None):
        return notifications.notify(
            customer_sub=sub, kind=NotificationKind.BOOKING_CONFIRMED,
            context={"car_label": "2008 Daihatsu Tanto X"},
            dedupe_key=dedupe, now=when or self.now,
        )

    def test_a_notification_stores_context_not_words(self):
        row, created = self.notify()
        self.assertTrue(created)
        self.assertEqual(row.context["car_label"], "2008 Daihatsu Tanto X")
        self.assertEqual(row.get_kind_display(), "Test drive confirmed")

    def test_the_same_event_notifies_once(self):
        first, created_a = self.notify()
        second, created_b = self.notify()
        self.assertTrue(created_a)
        self.assertFalse(created_b)
        self.assertEqual(first.sk, second.sk)
        self.assertEqual(len(notifications.recent("alice")), 1)

    def test_a_blank_dedupe_key_never_collapses_two_events(self):
        self.notify(dedupe="")
        self.notify(dedupe="", when=self.now + dt.timedelta(seconds=1))
        self.assertEqual(len(notifications.recent("alice")), 2)
        # ...and writes no guard, which an empty key attribute could not hold anyway.
        guards = list(DedupeGuard.query(keys.customer_pk("alice")))
        self.assertEqual(guards, [])

    def test_the_feed_is_newest_first(self):
        for i in range(3):
            self.notify(dedupe=f"e{i}", when=self.now + dt.timedelta(minutes=i))
        feed = notifications.recent("alice")
        self.assertEqual([n.dedupe_key for n in feed], ["e2", "e1", "e0"])

    def test_unread_count_and_marking_read(self):
        for i in range(3):
            self.notify(dedupe=f"e{i}", when=self.now + dt.timedelta(minutes=i))
        self.assertEqual(notifications.unread_count("alice"), 3)
        notifications.mark_read("alice", now=self.now)
        self.assertEqual(notifications.unread_count("alice"), 0)

    def test_marking_one_read_leaves_the_others(self):
        rows = [self.notify(dedupe=f"e{i}", when=self.now + dt.timedelta(minutes=i))[0]
                for i in range(3)]
        notifications.mark_read("alice", ids=[rows[1].notification_id], now=self.now)
        self.assertEqual(notifications.unread_count("alice"), 2)

    def test_notifications_carry_a_ttl_so_nothing_sweeps_on_a_timer(self):
        row, _ = self.notify()
        self.assertGreater(row.ttl, int(self.now.timestamp()))

    def test_one_customers_bell_is_not_anothers(self):
        self.notify(sub="alice")
        self.notify(sub="bob")
        self.assertEqual(len(notifications.recent("alice")), 1)
        self.assertEqual(len(notifications.recent("bob")), 1)


class CustomerStoreTests(DynamoTestCase):
    def setUp(self):
        super().setUp()
        self.now = dt.datetime.now(UTC)

    def test_ensure_is_idempotent_and_never_resets_the_counter(self):
        customers.ensure(sub="alice", email="Alice@Example.com", now=self.now)
        customers.get("alice").update(
            actions=[type(customers.get("alice")).active_bookings.set(2)])
        customers.ensure(sub="alice", email="Alice@Example.com", now=self.now)
        self.assertEqual(customers.get("alice").active_bookings, 2)

    def test_email_is_stored_lowercased_for_the_staff_list(self):
        customers.ensure(sub="alice", email="Alice@Example.com", now=self.now)
        self.assertEqual(customers.get("alice").email, "alice@example.com")

    def test_the_staff_list_is_alphabetical_by_email(self):
        for sub, email in [("c", "carol@x.com"), ("a", "alice@x.com"), ("b", "bob@x.com")]:
            customers.ensure(sub=sub, email=email, now=self.now)
        self.assertEqual([c.email for c in customers.all_customers()],
                         ["alice@x.com", "bob@x.com", "carol@x.com"])


class SearchTests(DynamoTestCase):
    def setUp(self):
        super().setUp()
        cars.clear_slug_cache()
        self.now = dt.datetime.now(UTC)
        cars.create(make_car("1", brand="Daihatsu", model_name="Tanto", grade="X",
                             chassis="L375S-0012345"), now=self.now)
        cars.create(make_car("2", brand="Toyota", model_name="Alphard", grade="G",
                             chassis="ANH20-9998888"), now=self.now)
        cars.create(make_car("3", brand="Toyota", model_name="Prius", grade="S",
                             chassis="ZVW30-1112222", status="sold"), now=self.now)

    def test_searching_by_partial_chassis_number_finds_the_car(self):
        """How a mechanic actually looks a car up."""
        found = search.find_cars(term="9998888")
        self.assertEqual([c.car_id for c in found], ["2"])

    def test_search_is_case_insensitive_across_fields(self):
        self.assertEqual([c.car_id for c in search.find_cars(term="tanto")], ["1"])
        self.assertEqual([c.car_id for c in search.find_cars(term="ALPHARD")], ["2"])

    def test_filtering_by_status_selects_the_partition(self):
        found = search.find_cars(status="sold")
        self.assertEqual([c.car_id for c in found], ["3"])

    def test_filtering_by_brand_narrows_within_a_status(self):
        found = search.find_cars(brand="Toyota")
        self.assertEqual({c.car_id for c in found}, {"2", "3"})

    def test_an_empty_term_returns_everything(self):
        self.assertEqual(len(search.find_cars()), 3)

    def test_questions_are_searchable_by_their_text_and_their_car(self):
        car = cars.get("1")
        questions.create(
            car_id=car.car_id, car_brand=car.brand, car_model_name=car.model_name,
            car_slug=car.slug, car_label=str(car),
            customer_sub="alice", customer_email="alice@example.com",
            question="Does it have a tow bar?", now=self.now)
        self.assertEqual(len(search.find_questions(term="tow bar")), 1)
        self.assertEqual(len(search.find_questions(brand="Daihatsu")), 1)
        self.assertEqual(len(search.find_questions(brand="Toyota")), 0)
