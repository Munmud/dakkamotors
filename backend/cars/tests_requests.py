"""Car requests: the store, the open endpoint, and the staff pages.

The endpoint is the one public write on the site that needs no account, so most of
what is asserted here is what a guest must and must not be able to do with it.
"""

import json
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from . import requests as domain
from .choices import RequestStatus
from .store import requests as store
from .store.errors import NotFound
from .tests import ClearsThrottleMixin, DynamoReset, MAIL_SETTINGS, make_customer
from .tests_fake_cognito import FakeCognito, sign_in
from .tests_staff import make_staff, make_staff_without_permissions

WISH = "A white Tanto, 2018 or newer, under 900,000. Sliding doors on both sides."


def submit(client, **payload):
    """POST the form, capturing what was queued for staff."""
    with mock.patch("cars.mail.boto3.client") as s3:
        response = client.post("/api/requests/", payload,
                               content_type="application/json")
        calls = s3.return_value.put_object.call_args_list
    sent = [json.loads(c.kwargs["Body"].decode("utf-8")) for c in calls]
    return response, sent


class RequestStoreTests(DynamoReset, SimpleTestCase):
    def test_the_queue_is_newest_first(self):
        first = store.create(name="A", email="a@example.com", phone="1", details="one")
        second = store.create(name="B", email="b@example.com", phone="2", details="two")

        ids = [r.request_id for r in store.queue()]

        self.assertEqual(ids, [second.request_id, first.request_id])

    def test_resolving_and_reopening(self):
        record = store.create(name="A", email="a@example.com", phone="1", details="one")

        store.resolve(record, staff_sub="staff-1", note="Found one")
        again = store.get(record.request_id)
        self.assertEqual(again.status, RequestStatus.RESOLVED)
        self.assertEqual(again.resolved_by, "staff-1")
        self.assertEqual(again.resolution_note, "Found one")
        self.assertEqual(store.queue(RequestStatus.OPEN), [])

        store.reopen(again)
        self.assertTrue(store.get(record.request_id).is_open)
        self.assertIsNone(store.get(record.request_id).resolved_at)

    def test_an_unknown_request_is_not_found(self):
        with self.assertRaises(NotFound):
            store.get("nope")


@override_settings(**MAIL_SETTINGS)
class RequestEndpointTests(FakeCognito, DynamoReset, ClearsThrottleMixin, SimpleTestCase):
    def test_a_guest_can_ask_with_their_details(self):
        response, sent = submit(
            self.client, name="Hana Sato", email="Hana@Example.com",
            phone="080-1234-5678", details=WISH, language="ja",
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertFalse(response.json()["signed_in"])
        (record,) = store.queue()
        self.assertEqual(record.name, "Hana Sato")
        self.assertEqual(record.email, "hana@example.com")
        self.assertEqual(record.phone, "080-1234-5678")
        self.assertEqual(record.details, WISH)
        self.assertEqual(record.language, "ja")
        self.assertIsNone(record.customer_sub)
        self.assertTrue(record.is_open)

        # Staff hear about it, with a way to reach the person, and the person hears
        # nothing -- there is nothing to tell them yet.
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["to"], ["staff@example.com"])
        self.assertIn("Car request from Hana Sato", sent[0]["subject"])
        self.assertIn("080-1234-5678", sent[0]["text"])
        self.assertIn(WISH, sent[0]["text"])

    def test_a_guest_without_a_phone_is_asked_for_one(self):
        response, sent = submit(self.client, name="Hana", email="h@example.com",
                                details=WISH)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "We need a name, an email address and a phone number so we can get back "
            "to you.",
        )
        self.assertEqual(store.queue(), [])
        self.assertEqual(sent, [])

    def test_a_signed_in_customer_gives_only_the_wish(self):
        user, _ = make_customer("buyer@example.com", phone="090-0000-1111",
                                name="Ken Buyer")
        sign_in(self.client, user)

        response, sent = submit(self.client, details=WISH)

        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()["signed_in"])
        (record,) = store.queue()
        self.assertEqual(record.name, "Ken Buyer")
        self.assertEqual(record.email, "buyer@example.com")
        self.assertEqual(record.phone, "090-0000-1111")
        self.assertEqual(record.customer_sub, user.sub)

    def test_a_signed_in_customer_cannot_impersonate_by_posting_details(self):
        """Whatever the body says, a signed-in request is filed under the account."""
        user, _ = make_customer("buyer@example.com", phone="090-0000-1111",
                                name="Ken Buyer")
        sign_in(self.client, user)

        submit(self.client, name="Somebody Else", email="else@example.com",
               phone="000", details=WISH)

        (record,) = store.queue()
        self.assertEqual(record.email, "buyer@example.com")
        self.assertEqual(record.name, "Ken Buyer")

    def test_an_empty_wish_is_refused(self):
        response, _ = submit(self.client, name="Hana", email="h@example.com",
                             phone="1", details="   ")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"],
                         "Tell us about the car you are looking for first.")

    def test_an_essay_is_refused(self):
        response, _ = submit(self.client, name="Hana", email="h@example.com",
                             phone="1", details="x" * (domain.MAX_DETAILS_LENGTH + 1))
        self.assertEqual(response.status_code, 400)
        self.assertIn("characters", response.json()["detail"])

    def test_a_bad_address_is_refused(self):
        response, _ = submit(self.client, name="Hana", email="not-an-address",
                             phone="1", details=WISH)
        self.assertEqual(response.status_code, 400)


class StaffRequestPageTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.open = store.create(name="Hana Sato", email="hana@example.com",
                                 phone="080-1234-5678", details=WISH)
        self.done = store.create(name="Old One", email="old@example.com",
                                 phone="1", details="A van")
        store.resolve(self.done, staff_sub="x", note="Sold them one")
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)

    def test_the_list_shows_open_requests_by_default(self):
        html = self.client.get(reverse("staff:request-list")).content.decode()

        self.assertIn("Hana Sato", html)
        self.assertNotIn("Old One", html)
        self.assertIn("1 open", html)

    def test_the_list_can_show_the_resolved_ones(self):
        html = self.client.get(reverse("staff:request-list"),
                               {"state": "resolved"}).content.decode()
        self.assertIn("Old One", html)
        self.assertNotIn("Hana Sato", html)

    def test_the_detail_page_prints_the_wish_and_the_contact(self):
        html = self.client.get(
            reverse("staff:request-detail", args=[self.open.request_id])
        ).content.decode()

        self.assertIn(WISH, html)
        self.assertIn('href="tel:080-1234-5678"', html)
        self.assertIn('href="mailto:hana@example.com"', html)
        # The block meant for a social post carries the wish and never the person.
        copy = html.split("Copy for a post")[1].split("</textarea>")[0]
        self.assertIn(WISH, copy)
        self.assertNotIn("Hana", copy)
        self.assertNotIn("080-1234", copy)

    def test_a_manager_can_resolve_and_reopen(self):
        url = reverse("staff:request-detail", args=[self.open.request_id])

        response = self.client.post(url, {"action": "resolve", "note": "Found a Tanto"})
        self.assertEqual(response.status_code, 302)
        record = store.get(self.open.request_id)
        self.assertEqual(record.status, RequestStatus.RESOLVED)
        self.assertEqual(record.resolution_note, "Found a Tanto")
        self.assertEqual(record.resolved_by, self.staff.sub)

        self.client.post(url, {"action": "reopen"})
        self.assertTrue(store.get(self.open.request_id).is_open)

    def test_no_role_means_no_page(self):
        nobody = make_staff_without_permissions()
        sign_in(self.client, nobody, staff=True)
        self.assertEqual(self.client.get(reverse("staff:request-list")).status_code, 403)

    def test_the_masthead_links_to_requests(self):
        html = self.client.get(reverse("staff:request-list")).content.decode()
        self.assertIn(">Requests<", html)
