"""The customer account flows, end to end, on Cognito.

Replaces the classes deleted from `tests.py` -- they asserted Django's auth internals
(`User.objects`, `PendingRegistration`, `check_password`, the session), none of which the
app uses any more. What they were *protecting* is asserted here against the real
endpoints: no usable account until the link is clicked, the raw token never stored, one
answer for every sign-in failure, the address not editable, and a reset link that dies
the moment the password changes.
"""

import json
from unittest import mock

from django.test import TestCase, override_settings

from . import cognito
from .authentication import ACCESS_COOKIE, REFRESH_COOKIE
from .store import auth as auth_store
from .store import customers as customer_store
from .tests import ClearsThrottleMixin, DynamoReset, MAIL_SETTINGS
from .tests_cognito import CognitoBackend


@override_settings(**MAIL_SETTINGS)
class AuthFlowTestCase(CognitoBackend, DynamoReset, ClearsThrottleMixin, TestCase):
    """A local Cognito, a local DynamoDB, and the real HTTP endpoints in front of both.

    `ClearsThrottleMixin` is not optional here: these endpoints are rate limited at
    20/hour and DRF keeps the counter in Django's cache for the life of the process, so
    without it the later tests in a run fail with 429 for reasons that have nothing to
    do with what they assert.
    """

    @staticmethod
    def recipients(message):
        """`to` is a list for the staff alert and a bare string for a customer."""
        to = message["to"]
        return to if isinstance(to, list) else [to]

    def register(self, email="new@example.com", password="simple", **extra):
        payload = {"name": "Yuki Tanaka", "email": email, "phone": "080-1234-5678",
                   "password": password}
        payload.update(extra)
        with mock.patch("cars.mail.boto3.client") as client:
            response = self.client.post("/api/auth/register/", payload,
                                        content_type="application/json")
            calls = client.return_value.put_object.call_args_list
        self.sent = [json.loads(c.kwargs["Body"].decode("utf-8")) for c in calls]
        return response

    def link_token(self):
        """The raw token out of the email, which is the only place it exists."""
        body = self.sent[-1]["text"]
        return body.split("token=")[1].split()[0].strip()

    def sign_in(self, email="new@example.com", password="simple"):
        return self.client.post("/api/auth/login/",
                                {"email": email, "password": password},
                                content_type="application/json")


class RegistrationTests(AuthFlowTestCase):
    def test_registering_leaves_the_account_unconfirmed(self):
        """Only the status is asserted, not that sign-in is refused.

        **moto lets an UNCONFIRMED user authenticate; real Cognito raises
        UserNotConfirmedException**, which `cognito.authenticate` catches and turns into
        the same "email or password is incorrect" every other failure gets. Asserting
        the refusal here would pass or fail on the stand-in's fidelity rather than on
        this code, so it is left to the real service.
        """
        response = self.register()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(cognito.status_of("new@example.com"), "UNCONFIRMED")

    def test_registering_does_not_sign_anyone_in(self):
        self.register()
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 403)

    def test_the_link_is_emailed_and_only_its_hash_is_stored(self):
        self.register()

        raw = self.link_token()
        pending = auth_store.find_registration("new@example.com")

        self.assertEqual(self.recipients(self.sent[-1]), ["new@example.com"])
        self.assertNotEqual(pending.token_hash, raw)
        self.assertEqual(pending.token_hash, auth_store.hash_token(raw))

    def test_the_password_is_never_stored_by_us_at_all(self):
        """Better than the table this replaces, which held a hash for three days.

        Cognito takes the password at sign-up and we never see it again.
        """
        self.register()

        pending = auth_store.find_registration("new@example.com")

        self.assertFalse(hasattr(pending, "password_hash"))
        self.assertNotIn("simple", json.dumps(self.sent))

    def test_an_address_that_already_has_an_account_is_told_to_sign_in(self):
        self.make_customer(email="taken@example.com")

        response = self.register(email="taken@example.com")

        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", json.dumps(response.json()))

    def test_registering_twice_replaces_the_unconfirmed_attempt(self):
        """A typo on the first try must not lock the address out for three days."""
        self.register()
        first = self.link_token()

        self.register()
        second = self.link_token()

        self.assertNotEqual(first, second)
        self.assertIsNone(auth_store.registration_for_token(first))
        self.assertIsNotNone(auth_store.registration_for_token(second))

    def test_a_short_password_is_refused_with_a_reason(self):
        response = self.register(password="abc")

        self.assertEqual(response.status_code, 400)
        self.assertIn("at least 6", json.dumps(response.json()))

    def test_customers_may_use_an_easy_password(self):
        """Length alone, as before. The rate limit does the work complexity does not."""
        self.assertEqual(self.register(password="simple").status_code, 202)


class VerificationTests(AuthFlowTestCase):
    def test_the_link_confirms_the_account_and_sends_them_to_sign_in(self):
        self.register()

        response = self.client.post("/api/auth/verify/",
                                    {"token": self.link_token()},
                                    content_type="application/json")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["verified"])
        self.assertEqual(body["email"], "new@example.com")
        self.assertEqual(cognito.status_of("new@example.com"), "CONFIRMED")

    def test_verifying_does_not_sign_them_in(self):
        """Changed on purpose: Cognito holds the password and we never see it again.

        The alternative was keeping a recoverable password for three days, which is
        worse than one extra screen.
        """
        self.register()
        self.client.post("/api/auth/verify/", {"token": self.link_token()},
                         content_type="application/json")

        self.assertEqual(self.client.get("/api/auth/me/").status_code, 403)

    def test_the_customer_can_sign_in_afterwards(self):
        self.register()
        self.client.post("/api/auth/verify/", {"token": self.link_token()},
                         content_type="application/json")

        response = self.sign_in()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], "new@example.com")

    def test_the_same_link_cannot_be_used_twice(self):
        self.register()
        token = self.link_token()
        self.client.post("/api/auth/verify/", {"token": token},
                         content_type="application/json")

        again = self.client.post("/api/auth/verify/", {"token": token},
                                 content_type="application/json")

        self.assertEqual(again.status_code, 400)

    def test_an_unknown_link_is_refused(self):
        response = self.client.post("/api/auth/verify/", {"token": "made-up"},
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_they_are_returned_to_the_booking_they_started(self):
        self.register(next="/cars/a-car/test-drive")

        body = self.client.post("/api/auth/verify/", {"token": self.link_token()},
                                content_type="application/json").json()

        self.assertEqual(body["next"], "/cars/a-car/test-drive")

    def test_an_offsite_next_is_discarded(self):
        for hostile in ("//evil.com", "https://evil.com", "javascript:alert(1)"):
            with self.subTest(hostile=hostile):
                cognito.delete_user("new@example.com")
                auth_store.discard_registration("new@example.com")
                self.register(next=hostile)

                body = self.client.post("/api/auth/verify/",
                                        {"token": self.link_token()},
                                        content_type="application/json").json()

                self.assertEqual(body["next"], "/account")

    def test_verifying_creates_the_counter_the_booking_limit_uses(self):
        self.register()
        self.client.post("/api/auth/verify/", {"token": self.link_token()},
                         content_type="application/json")

        attrs = cognito.attributes_of("new@example.com")
        customer = customer_store.find(attrs["sub"])

        self.assertIsNotNone(customer)
        self.assertEqual(customer.active_bookings, 0)

    def test_resending_sends_a_fresh_link(self):
        self.register()
        first = self.link_token()

        with mock.patch("cars.mail.boto3.client") as client:
            self.client.post("/api/auth/resend/", {"email": "new@example.com"},
                             content_type="application/json")
            calls = client.return_value.put_object.call_args_list
        self.sent = [json.loads(c.kwargs["Body"].decode("utf-8")) for c in calls]

        self.assertNotEqual(self.link_token(), first)

    def test_resending_for_an_unknown_address_says_the_same_thing_and_emails_nobody(self):
        with mock.patch("cars.mail.boto3.client") as client:
            response = self.client.post("/api/auth/resend/",
                                        {"email": "nobody@example.com"},
                                        content_type="application/json")
            calls = client.return_value.put_object.call_args_list

        self.assertEqual(response.status_code, 202)
        self.assertEqual(calls, [])


class SessionTests(AuthFlowTestCase):
    def setUp(self):
        super().setUp()
        self.email, self.password = self.make_customer(email="signin@example.com")

    def test_signing_in_sets_httponly_cookies(self):
        response = self.sign_in(self.email, self.password)

        self.assertEqual(response.status_code, 200)
        access = response.cookies[ACCESS_COOKIE]
        self.assertTrue(access["httponly"])
        self.assertEqual(access["samesite"], "Lax")
        self.assertEqual(access["path"], "/api")
        # Scoped tighter, so it is not attached to ordinary API calls.
        self.assertEqual(response.cookies[REFRESH_COOKIE]["path"], "/api/auth")

    def test_the_cookie_authenticates_subsequent_requests(self):
        self.sign_in(self.email, self.password)

        me = self.client.get("/api/auth/me/")

        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], self.email)
        self.assertEqual(me.json()["name"], "Aiko Tanaka")
        self.assertEqual(me.json()["phone"], "080-1111-2222")
        self.assertFalse(me.json()["is_staff"])

    # An unconfirmed account being refused is deliberately not tested here: moto allows
    # it where Cognito does not, so the assertion would measure the stand-in. The
    # handler that turns UserNotConfirmedException into the shared message lives in
    # `cognito.authenticate` alongside the other two refusals it catches.

    def test_a_wrong_password_does_not_reveal_whether_the_account_exists(self):
        known = self.sign_in(self.email, "wrong-password")
        unknown = self.sign_in("nobody@example.com", "wrong-password")

        self.assertEqual(known.status_code, 400)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(known.json()["detail"], unknown.json()["detail"])

    def test_signing_out_clears_the_cookies(self):
        self.sign_in(self.email, self.password)

        response = self.client.post("/api/auth/logout/")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.cookies[ACCESS_COOKIE].value, "")
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 403)

    def test_a_guest_is_nobody(self):
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 403)

    def test_the_csrf_endpoint_still_works_without_a_session(self):
        """Worth asserting: CSRF needs no database, which is why it survived."""
        response = self.client.get("/api/auth/csrf/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["csrfToken"])


class ProfileTests(AuthFlowTestCase):
    def setUp(self):
        super().setUp()
        self.email, self.password = self.make_customer(email="me@example.com")
        self.sign_in(self.email, self.password)

    def patch(self, **data):
        return self.client.patch("/api/auth/me/", data,
                                 content_type="application/json")

    def test_a_customer_can_fix_their_own_name_and_phone(self):
        response = self.patch(first_name="Aiko", last_name="Suzuki",
                              phone="090-0000-1111")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Aiko Suzuki")
        self.assertEqual(response.json()["phone"], "090-0000-1111")
        self.assertEqual(cognito.attributes_of(self.email)["family_name"], "Suzuki")

    def test_the_email_cannot_be_changed_here(self):
        """A security control, not a UI nicety: the address is the sign-in name."""
        response = self.patch(email="someone.else@example.com")

        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be changed here", response.json()["detail"])
        self.assertEqual(cognito.attributes_of(self.email)["email"], self.email)

    def test_a_blank_phone_is_refused(self):
        self.assertEqual(self.patch(phone="   ").status_code, 400)

    def test_a_guest_cannot_edit_anyone(self):
        self.client.post("/api/auth/logout/")
        self.assertEqual(self.patch(first_name="Nobody").status_code, 403)


class PasswordResetTests(AuthFlowTestCase):
    def setUp(self):
        super().setUp()
        self.email, self.password = self.make_customer(email="forgot@example.com")

    def ask_for_link(self, email=None):
        with mock.patch("cars.mail.boto3.client") as client:
            response = self.client.post("/api/auth/password-reset/",
                                        {"email": email or self.email},
                                        content_type="application/json")
            calls = client.return_value.put_object.call_args_list
        self.sent = [json.loads(c.kwargs["Body"].decode("utf-8")) for c in calls]
        return response

    def test_a_customer_gets_a_link(self):
        response = self.ask_for_link()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.recipients(self.sent[0]), [self.email])

    def test_an_unknown_address_gets_the_same_answer_and_no_email(self):
        response = self.ask_for_link("nobody@example.com")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.sent, [])

    def test_a_staff_address_gets_no_link(self):
        """Anyone who can read that inbox could otherwise take over an account that
        edits inventory and other staff."""
        cognito.add_to_group(self.email, cognito.STAFF_GROUP)

        self.ask_for_link()

        self.assertEqual(self.sent, [])

    def test_the_link_sets_a_new_password_and_signs_them_in(self):
        self.ask_for_link()
        token = self.link_token()

        response = self.client.post("/api/auth/password-reset/confirm/",
                                    {"token": token, "password": "brand-new-1"},
                                    content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertIn(ACCESS_COOKIE, response.cookies)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 200)
        self.assertIsNotNone(
            cognito.authenticate(email=self.email, password="brand-new-1"))

    def test_the_same_link_cannot_be_used_twice(self):
        self.ask_for_link()
        token = self.link_token()
        self.client.post("/api/auth/password-reset/confirm/",
                         {"token": token, "password": "brand-new-1"},
                         content_type="application/json")

        again = self.client.post("/api/auth/password-reset/confirm/",
                                 {"token": token, "password": "another-one-2"},
                                 content_type="application/json")

        self.assertEqual(again.status_code, 400)

    def test_every_outstanding_link_dies_with_the_reset(self):
        """What deriving the token from the password hash used to do for free."""
        self.ask_for_link()
        first = self.link_token()
        self.ask_for_link()
        second = self.link_token()

        self.client.post("/api/auth/password-reset/confirm/",
                         {"token": second, "password": "brand-new-1"},
                         content_type="application/json")

        stale = self.client.post("/api/auth/password-reset/confirm/",
                                 {"token": first, "password": "another-one-2"},
                                 content_type="application/json")

        self.assertEqual(stale.status_code, 400)

    def test_a_reset_password_may_be_easy_but_not_tiny(self):
        self.ask_for_link()

        response = self.client.post("/api/auth/password-reset/confirm/",
                                    {"token": self.link_token(), "password": "abc"},
                                    content_type="application/json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("at least 6", response.json()["detail"])

    def test_an_unknown_link_is_refused(self):
        response = self.client.post("/api/auth/password-reset/confirm/",
                                    {"token": "made-up", "password": "brand-new-1"},
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
