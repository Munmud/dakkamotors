"""The Cognito custom-auth trigger that turns the sign-up code into a sign-in.

Extracted from `infra/data.yaml` like the migration trigger, and for the same reason:
the thing that will run is the thing that was checked. What matters here is narrower
than for the password carry-over and more dangerous if wrong -- this trigger decides
who gets a session without a password, so every refusal is a test.
"""

import hashlib

from django.test import SimpleTestCase

from .store import keys
from .tests_user_migration import load_trigger

SOURCE = {
    "define": "DefineAuthChallenge_Authentication",
    "create": "CreateAuthChallenge_Authentication",
    "verify": "VerifyAuthChallengeResponse_Authentication",
}


def event(source, *, session=None, answer=None, email="buyer@example.com",
          user_name="1b2c3d4e-sub"):
    return {
        "triggerSource": source,
        # The sub, not the address: the pool uses email as the username attribute,
        # which is the trap `verify` exists to step around.
        "userName": user_name,
        "request": {
            "session": session or [],
            "challengeAnswer": answer,
            "userAttributes": {"email": email, "sub": user_name},
        },
        "response": {},
    }


class RecordingDdb:
    """Answers one GetItem and remembers what it was asked for."""

    def __init__(self, item=None):
        self.item = item
        self.keys = []

    def get_item(self, **kwargs):
        self.keys.append(kwargs["Key"])
        return {"Item": self.item} if self.item else {}


class DefineChallengeTests(SimpleTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.trigger = load_trigger("LinkAuthFunction")

    def test_a_fresh_session_is_asked_for_the_code(self):
        out = self.trigger.handler(event(SOURCE["define"]), None)["response"]
        self.assertEqual(out["challengeName"], "CUSTOM_CHALLENGE")
        self.assertFalse(out["issueTokens"])
        self.assertFalse(out["failAuthentication"])

    def test_a_correct_answer_issues_tokens(self):
        session = [{"challengeName": "CUSTOM_CHALLENGE", "challengeResult": True}]
        out = self.trigger.handler(event(SOURCE["define"], session=session),
                                   None)["response"]
        self.assertTrue(out["issueTokens"])
        self.assertFalse(out["failAuthentication"])

    def test_a_wrong_answer_ends_the_attempt(self):
        """One try. The token is 256 bits; a retry loop would only serve a guesser."""
        session = [{"challengeName": "CUSTOM_CHALLENGE", "challengeResult": False}]
        out = self.trigger.handler(event(SOURCE["define"], session=session),
                                   None)["response"]
        self.assertFalse(out["issueTokens"])
        self.assertTrue(out["failAuthentication"])

    def test_it_never_issues_a_password_challenge(self):
        """The customer client has no password flow a browser can drive, and enabling
        CUSTOM_AUTH must not open one by the back door."""
        for session in ([], [{"challengeName": "SRP_A", "challengeResult": True}],
                        [{"challengeName": "PASSWORD_VERIFIER",
                          "challengeResult": True}]):
            out = self.trigger.handler(event(SOURCE["define"], session=session),
                                       None)["response"]
            self.assertNotIn(out.get("challengeName"),
                             ("SRP_A", "PASSWORD_VERIFIER"))
            if session:
                # A password step that somehow appears in the session is not a
                # passed custom challenge, so it does not earn tokens either.
                self.assertFalse(out["issueTokens"])


class CreateChallengeTests(SimpleTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.trigger = load_trigger("LinkAuthFunction")

    def test_nothing_secret_is_generated(self):
        """The secret is already in the customer's inbox. Anything put in the public
        parameters would be handed to whoever started the flow."""
        out = self.trigger.handler(event(SOURCE["create"]), None)["response"]
        self.assertEqual(out["publicChallengeParameters"], {"kind": "email-code"})
        self.assertEqual(out["privateChallengeParameters"], {})


class VerifyAnswerTests(SimpleTestCase):
    """The answer is the sign-up code; the pending item, read by address, holds its
    hash. Never a lookup by the code: six digits are not unique across customers."""

    CODE = "482913"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.trigger = load_trigger("LinkAuthFunction")

    def pending_item(self, email="buyer@example.com", code=None):
        digest = hashlib.sha256((code or self.CODE).encode()).hexdigest()
        return {"pk": {"S": keys.pending_pk(email)}, "sk": {"S": "META"},
                "email": {"S": email}, "token_hash": {"S": digest}}

    def verify(self, ddb, answer=None, **kwargs):
        self.trigger.ddb = ddb
        return self.trigger.handler(
            event(SOURCE["verify"], answer=answer or self.CODE, **kwargs), None
        )["response"]["answerCorrect"]

    def test_the_right_code_is_let_in(self):
        self.assertTrue(self.verify(RecordingDdb(self.pending_item())))

    def test_the_lookup_is_by_address_with_the_key_the_store_uses(self):
        """A Lambda cannot import store/keys.py, so this is the only thing keeping
        the two spellings of PENDING# together. The address is lowercased first,
        as `pending_pk` lowercases it."""
        ddb = RecordingDdb(self.pending_item())
        self.verify(ddb, email="Buyer@Example.com")
        self.assertEqual(ddb.keys, [{"pk": {"S": keys.pending_pk("buyer@example.com")},
                                     "sk": {"S": keys.META}}])
        self.assertNotIn(self.CODE, ddb.keys[0]["pk"]["S"])

    def test_a_wrong_code_is_refused(self):
        self.assertFalse(self.verify(RecordingDdb(self.pending_item()), answer="000000"))

    def test_the_code_is_compared_as_a_hash(self):
        """The item holds a hash. A raw code stored by mistake must not match itself."""
        item = self.pending_item()
        item["token_hash"] = {"S": self.CODE}
        self.assertFalse(self.verify(RecordingDdb(item)))

    def test_another_customers_code_is_useless_here(self):
        """The item read is the *signing-in* address's, so a code issued to somebody
        else is simply not the hash on it -- there is no comparison to get wrong."""
        ddb = RecordingDdb(self.pending_item("buyer@example.com", code="111111"))
        self.assertFalse(self.verify(ddb, answer=self.CODE, email="buyer@example.com"))

    def test_no_pending_sign_up_is_refused(self):
        self.assertFalse(self.verify(RecordingDdb(None)))

    def test_an_empty_answer_never_reaches_the_table(self):
        ddb = RecordingDdb(self.pending_item())
        self.trigger.ddb = ddb
        out = self.trigger.handler(event(SOURCE["verify"], answer=""), None)
        self.assertFalse(out["response"]["answerCorrect"])
        self.assertEqual(ddb.keys, [])


class TriggerContractTests(SimpleTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.trigger = load_trigger("LinkAuthFunction")

    def test_an_unsupported_trigger_source_is_refused(self):
        with self.assertRaises(Exception):
            self.trigger.handler(event("PreSignUp_SignUp"), None)
