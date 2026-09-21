"""The Cognito custom-auth trigger that turns the verification link into a sign-in.

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

    def test_a_fresh_session_is_asked_for_the_link_token(self):
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
        self.assertEqual(out["publicChallengeParameters"], {"kind": "email-link"})
        self.assertEqual(out["privateChallengeParameters"], {})


class VerifyAnswerTests(SimpleTestCase):

    RAW = "the-raw-token-from-the-email"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.trigger = load_trigger("LinkAuthFunction")

    def pending_item(self, email="buyer@example.com"):
        return {"pk": {"S": keys.pending_token_pk(
                    hashlib.sha256(self.RAW.encode()).hexdigest())},
                "sk": {"S": "META"}, "email": {"S": email}}

    def verify(self, ddb, **kwargs):
        self.trigger.ddb = ddb
        return self.trigger.handler(
            event(SOURCE["verify"], answer=self.RAW, **kwargs), None
        )["response"]["answerCorrect"]

    def test_the_link_holder_is_let_in(self):
        self.assertTrue(self.verify(RecordingDdb(self.pending_item())))

    def test_the_lookup_is_by_hash_and_the_key_the_store_uses(self):
        """The raw token is never stored, so the trigger must hash it, and it must build
        the same key `store/keys.py` does -- a Lambda cannot import the package, so
        this is the only thing keeping the two spellings together."""
        ddb = RecordingDdb(self.pending_item())
        self.verify(ddb)
        digest = hashlib.sha256(self.RAW.encode("utf-8")).hexdigest()
        self.assertEqual(ddb.keys, [{"pk": {"S": keys.pending_token_pk(digest)},
                                     "sk": {"S": keys.META}}])
        self.assertNotIn(self.RAW, ddb.keys[0]["pk"]["S"])

    def test_a_token_for_another_address_is_refused(self):
        """A real link, presented for the wrong account. The match is on the address
        in the user's attributes, not on `userName`, which is the sub."""
        self.assertFalse(self.verify(RecordingDdb(self.pending_item()),
                                     email="someone-else@example.com"))

    def test_the_address_comparison_is_case_insensitive(self):
        self.assertTrue(self.verify(RecordingDdb(self.pending_item("Buyer@Example.com")),
                                    email="buyer@example.com"))

    def test_an_unknown_token_is_refused(self):
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
