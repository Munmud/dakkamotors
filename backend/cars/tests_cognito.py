"""The Cognito machinery: the client wrapper, token verification, and the auth state
that stays in DynamoDB because Cognito has nowhere to put it.

Run against **moto in server mode**, not `mock_aws`. That matters: `mock_aws` patches
botocore globally, so it would swallow the DynamoDB Local calls the rest of the suite
depends on. A server on loopback intercepts only the client that is pointed at it, which
is the same shape as DynamoDB Local and keeps the two stand-ins independent.

Token signatures are verified for real. moto issues genuine RS256 tokens and ships the
matching public JWKS, so `authentication.verify` does the same work here that it will do
against Cognito -- including rejecting a token signed by something else.
"""

import datetime as dt
import gzip
import json
import pathlib
import tempfile
import unittest

import jwt
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

try:
    from moto.server import ThreadedMotoServer
    MOTO = True
except Exception:  # pragma: no cover - moto is a dev-only dependency
    MOTO = False

from . import authentication, cognito
from .store import auth as auth_store
from .tests_store import DynamoTestCase


def _jwks_path():
    """moto's public signing key, written where the JWKS loader can read it.

    Exercising the same "baked into the deploy" path production uses, rather than a
    special case: production points at a file too.
    """
    import moto

    source = (pathlib.Path(moto.__file__).parent / "cognitoidp" / "resources"
              / "jwks-public.json.gz")
    target = pathlib.Path(tempfile.gettempdir()) / "dakkamotors-test-jwks.json"
    target.write_bytes(gzip.decompress(source.read_bytes()))
    return str(target)


class CognitoBackend:
    """Starts a local Cognito, builds the pool this app expects, points settings at it.

    A mixin rather than a base class so it can sit in front of either `SimpleTestCase`
    (for the client wrapper) or `TestCase` (for the HTTP flows, which still need the ORM
    for staff accounts while auth is mid-migration).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Werkzeug logs a line per request, which is a few hundred lines of noise
        # across this module and hides anything that actually matters.
        import logging

        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        cls.server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
        cls.server.start()
        _, port = cls.server.get_host_and_port()
        cls.endpoint = f"http://127.0.0.1:{port}"
        cls.jwks = _jwks_path()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        import boto3

        raw = boto3.client(
            "cognito-idp", region_name="ap-northeast-1", endpoint_url=self.endpoint,
            aws_access_key_id="local", aws_secret_access_key="local")

        pool = raw.create_user_pool(
            PoolName="dakkamotors-test",
            UsernameAttributes=["email"],
            # Length alone, matching CUSTOMER_PASSWORD_VALIDATORS and the real pool.
            # A pool has exactly one password policy, and a stricter one would reject
            # every existing customer whose password is six or seven characters at the
            # moment the migration trigger tries to verify it -- with an error Cognito
            # words, on the sign-in screen. Mirrored here so the tests would notice if
            # the real policy drifted.
            Policies={"PasswordPolicy": {
                "MinimumLength": 6,
                "RequireUppercase": False,
                "RequireLowercase": False,
                "RequireNumbers": False,
                "RequireSymbols": False,
            }},
            Schema=[
                {"Name": "email", "AttributeDataType": "String",
                 "Required": True, "Mutable": True},
                {"Name": "given_name", "AttributeDataType": "String", "Mutable": True},
                {"Name": "family_name", "AttributeDataType": "String", "Mutable": True},
                # custom:phone, not the standard phone_number: Cognito validates that
                # one as E.164 and customers type 080-1111-2222.
                {"Name": "phone", "AttributeDataType": "String", "Mutable": True},
            ],
        )
        self.pool_id = pool["UserPool"]["Id"]

        self.customer_client = raw.create_user_pool_client(
            UserPoolId=self.pool_id, ClientName="customer", GenerateSecret=False,
            ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH",
                               "ALLOW_REFRESH_TOKEN_AUTH"],
        )["UserPoolClient"]["ClientId"]
        self.staff_client = raw.create_user_pool_client(
            UserPoolId=self.pool_id, ClientName="staff", GenerateSecret=False,
            ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH"],
        )["UserPoolClient"]["ClientId"]

        for group in (cognito.STAFF_GROUP, cognito.OWNERS_GROUP,
                      cognito.INVENTORY_GROUP):
            raw.create_group(UserPoolId=self.pool_id, GroupName=group)

        self.settings_patch = override_settings(
            COGNITO_ENDPOINT_URL=self.endpoint,
            COGNITO_POOL_ID=self.pool_id,
            COGNITO_CUSTOMER_CLIENT_ID=self.customer_client,
            COGNITO_STAFF_CLIENT_ID=self.staff_client,
            COGNITO_JWKS_PATH=self.jwks,
        )
        self.settings_patch.enable()
        self.addCleanup(self.settings_patch.disable)

        cognito.reset_client()
        authentication.reset_keys()
        self.addCleanup(cognito.reset_client)
        self.addCleanup(authentication.reset_keys)

        # Build the clients now, while nothing is patched.
        #
        # `mock.patch("cars.mail.boto3.client")` -- which most of the mail tests use --
        # replaces the attribute on the *shared* boto3 module, not on some private copy
        # belonging to `cars.mail`. Any client constructed while that patch is active is
        # a MagicMock, whichever service it was meant to talk to. Priming the cached
        # ones here means Cognito and DynamoDB already hold real clients by the time a
        # test patches the mail one.
        cognito.client()
        from .store.txn import connection

        connection()

    # -- helpers --------------------------------------------------------------------

    def make_customer(self, email="buyer@example.com", password="customer-pw-1234",
                      name="Aiko Tanaka", phone="080-1111-2222", confirm=True):
        cognito.sign_up(email=email, password=password, name=name, phone=phone)
        if confirm:
            cognito.confirm(email)
        return email, password


@unittest.skipUnless(MOTO, "moto is not installed; pip install -r requirements-dev.txt")
class CognitoTestCase(CognitoBackend, SimpleTestCase):
    databases = []


class SignUpTests(CognitoTestCase):
    def test_a_sign_up_leaves_the_account_unconfirmed(self):
        """The property the two-phase flow exists for.

        No usable account exists until the emailed link is clicked, so nothing
        downstream has to ask whether a customer is verified.

        Only the status is asserted, not the sign-in refusal. **moto lets an
        UNCONFIRMED user authenticate; real Cognito raises UserNotConfirmedException.**
        A test that asserted the refusal would pass here for the wrong reason and prove
        nothing -- `cognito.authenticate` catches that exception and returns None, which
        is the actual behaviour, and it is exercised against the real service or not at
        all.
        """
        email, _ = self.make_customer(confirm=False)

        self.assertEqual(cognito.status_of(email), "UNCONFIRMED")

    def test_confirming_lets_them_in(self):
        email, password = self.make_customer()

        self.assertEqual(cognito.status_of(email), "CONFIRMED")
        tokens = cognito.authenticate(email=email, password=password)
        self.assertIsNotNone(tokens)
        self.assertIn("AccessToken", tokens)
        self.assertIn("RefreshToken", tokens)

    def test_the_name_and_phone_are_stored_as_attributes(self):
        email, _ = self.make_customer()

        attrs = cognito.attributes_of(email)

        self.assertEqual(attrs["given_name"], "Aiko")
        self.assertEqual(attrs["family_name"], "Tanaka")
        self.assertEqual(attrs[cognito.PHONE_ATTR], "080-1111-2222")

    def test_signing_up_twice_is_reported_rather_than_silently_allowed(self):
        self.make_customer()
        with self.assertRaises(Exception) as caught:
            cognito.sign_up(email="buyer@example.com", password="other-pw-1234")
        self.assertIn("UsernameExists", type(caught.exception).__name__)

    def test_a_wrong_password_is_refused(self):
        email, _ = self.make_customer()
        self.assertIsNone(cognito.authenticate(email=email, password="wrong-one"))

    def test_an_unknown_address_is_refused_the_same_way(self):
        """One answer for both, so the endpoint cannot be used to find accounts."""
        self.assertIsNone(
            cognito.authenticate(email="nobody@example.com", password="whatever"))


class TokenVerificationTests(CognitoTestCase):
    def tokens_for(self, **kwargs):
        email, password = self.make_customer(**kwargs)
        return cognito.authenticate(email=email, password=password)

    def test_a_real_token_verifies_and_carries_the_subject(self):
        tokens = self.tokens_for()

        claims = authentication.verify(tokens["AccessToken"])

        self.assertEqual(claims["token_use"], "access")
        self.assertEqual(claims["client_id"], self.customer_client)
        self.assertTrue(claims["sub"])

    def test_the_username_claim_is_the_sub_not_the_email(self):
        """Worth pinning down, because assuming otherwise looks right and is not.

        With email as the username attribute, the pool's internal username is a UUID and
        the address is an attribute. Anything reading `username` off an access token and
        calling it an email gets a UUID.
        """
        tokens = self.tokens_for()

        claims = authentication.verify(tokens["AccessToken"])

        self.assertEqual(claims["username"], claims["sub"])
        self.assertNotIn("@", claims["username"])

    def test_a_token_signed_by_something_else_is_refused(self):
        forged = jwt.encode(
            {"sub": "attacker", "token_use": "access",
             "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)},
            "not-the-pools-key", algorithm="HS256")

        with self.assertRaises(Exception):
            authentication.verify(forged)

    def test_an_id_token_is_refused_even_though_it_is_genuine(self):
        """The ID token is for the client; its claims are not ours to trust."""
        tokens = self.tokens_for()

        with self.assertRaises(Exception):
            authentication.verify(tokens["IdToken"])

    def test_a_user_built_from_claims_knows_its_groups_without_a_lookup(self):
        email, password = self.make_customer()
        cognito.add_to_group(email, cognito.STAFF_GROUP)
        tokens = cognito.authenticate(email=email, password=password)

        user = cognito.CognitoUser.from_claims(
            authentication.verify(tokens["AccessToken"]))

        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_authenticated)

    def test_profile_attributes_are_fetched_only_when_asked_for(self):
        email, password = self.make_customer()
        tokens = cognito.authenticate(email=email, password=password)
        user = cognito.CognitoUser.from_claims(
            authentication.verify(tokens["AccessToken"]))

        # Nothing loaded yet: a booking needs only the sub.
        self.assertIsNone(user._attrs)

        self.assertEqual(user.email, email)
        self.assertEqual(user.get_full_name(), "Aiko Tanaka")
        self.assertEqual(user.phone, "080-1111-2222")


class GroupTests(CognitoTestCase):
    def test_groups_replace_is_staff_and_is_superuser(self):
        email, _ = self.make_customer()

        self.assertEqual(cognito.groups_of(email), [])
        cognito.add_to_group(email, cognito.STAFF_GROUP)
        cognito.add_to_group(email, cognito.INVENTORY_GROUP)

        self.assertEqual(sorted(cognito.groups_of(email)),
                         [cognito.INVENTORY_GROUP, cognito.STAFF_GROUP])

    def test_the_staff_list_is_the_group_membership(self):
        staff, _ = self.make_customer(email="staffer@example.com")
        self.make_customer(email="buyer@example.com")
        cognito.add_to_group(staff, cognito.STAFF_GROUP)

        members = cognito.users_in_group(cognito.STAFF_GROUP)

        self.assertEqual(len(members), 1)

    def test_removing_someone_from_staff_takes_the_flag_with_it(self):
        email, password = self.make_customer()
        cognito.add_to_group(email, cognito.STAFF_GROUP)
        cognito.remove_from_group(email, cognito.STAFF_GROUP)

        tokens = cognito.authenticate(email=email, password=password)
        user = cognito.CognitoUser.from_claims(
            authentication.verify(tokens["AccessToken"]))

        self.assertFalse(user.is_staff)


class ProfileTests(CognitoTestCase):
    def test_a_customer_can_change_their_name_and_phone(self):
        email, _ = self.make_customer()

        cognito.update_attributes(email, first_name="Aiko", last_name="Suzuki",
                                  phone="090-0000-1111")

        attrs = cognito.attributes_of(email)
        self.assertEqual(attrs["family_name"], "Suzuki")
        self.assertEqual(attrs[cognito.PHONE_ATTR], "090-0000-1111")

    def test_update_attributes_refuses_to_touch_the_email(self):
        """Not a UI nicety: the address is the username, and moving one without the
        other would let a customer take an address that belongs to someone else."""
        email, _ = self.make_customer()

        cognito.update_attributes(email, email="someone.else@example.com",
                                  first_name="Aiko")

        self.assertEqual(cognito.attributes_of(email)["email"], email)

    def test_a_user_can_be_rebuilt_from_its_subject_identifier(self):
        email, password = self.make_customer()
        tokens = cognito.authenticate(email=email, password=password)
        sub = authentication.verify(tokens["AccessToken"])["sub"]

        user = cognito.user_for(sub)

        self.assertEqual(user.email, email)
        self.assertEqual(user.get_full_name(), "Aiko Tanaka")

    def test_an_unknown_subject_identifier_is_nobody(self):
        self.assertIsNone(cognito.user_for("00000000-0000-0000-0000-000000000000"))


class AuthStateTests(DynamoTestCase):
    """The parts of the flows Cognito has nowhere to put."""

    def setUp(self):
        super().setUp()
        self.now = timezone.now()

    def test_a_pending_sign_up_holds_what_cognito_cannot(self):
        raw = auth_store.start_registration(
            email="Buyer@Example.com", name="Aiko Tanaka", phone="080-1111-2222",
            next_path="/cars/x/test-drive", language="ja", now=self.now)

        pending = auth_store.registration_for_token(raw)

        self.assertEqual(pending.email, "buyer@example.com")
        self.assertEqual(pending.phone, "080-1111-2222")
        self.assertEqual(pending.next_path, "/cars/x/test-drive")
        self.assertEqual(pending.language, "ja")
        # And, unlike the table it replaces, no password hash at all.
        self.assertFalse(hasattr(pending, "password_hash"))

    def test_only_the_hash_of_the_link_is_stored(self):
        raw = auth_store.start_registration(
            email="a@example.com", name="A", phone="1", now=self.now)

        pending = auth_store.find_registration("a@example.com")

        self.assertNotEqual(pending.token_hash, raw)
        self.assertEqual(pending.token_hash, auth_store.hash_token(raw))

    def test_resending_invalidates_the_previous_link(self):
        """Otherwise a resend leaves two working activation links for one address."""
        first = auth_store.start_registration(
            email="a@example.com", name="A", phone="1", now=self.now)
        second = auth_store.start_registration(
            email="a@example.com", name="A", phone="1", now=self.now)

        self.assertIsNone(auth_store.registration_for_token(first))
        self.assertIsNotNone(auth_store.registration_for_token(second))

    def test_an_unknown_link_resolves_to_nothing(self):
        self.assertIsNone(auth_store.registration_for_token("made-up"))

    def test_finishing_clears_both_items_so_the_link_is_single_use(self):
        raw = auth_store.start_registration(
            email="a@example.com", name="A", phone="1", now=self.now)

        auth_store.finish_registration(auth_store.registration_for_token(raw))

        self.assertIsNone(auth_store.registration_for_token(raw))
        self.assertIsNone(auth_store.find_registration("a@example.com"))

    # -- resets ---------------------------------------------------------------------

    def test_a_reset_link_resolves_to_its_account(self):
        raw = auth_store.start_reset(sub="sub-1", email="a@example.com", now=self.now)

        token = auth_store.reset_for_token(raw)

        self.assertEqual(token.sub, "sub-1")

    def test_completing_a_reset_kills_every_outstanding_link(self):
        """What deriving the token from the password hash used to do for free.

        Cognito never hands us the hash, so an epoch counter buys the same property:
        a link carries the epoch it was minted under, and a reset moves it.
        """
        first = auth_store.start_reset(sub="sub-1", email="a@example.com", now=self.now)
        second = auth_store.start_reset(sub="sub-1", email="a@example.com", now=self.now)

        auth_store.complete_reset(auth_store.reset_for_token(first))

        self.assertIsNone(auth_store.reset_for_token(first))
        self.assertIsNone(auth_store.reset_for_token(second),
                          "a sibling link must not outlive the reset")

    def test_one_customers_reset_does_not_affect_another(self):
        mine = auth_store.start_reset(sub="sub-1", email="a@example.com", now=self.now)
        theirs = auth_store.start_reset(sub="sub-2", email="b@example.com", now=self.now)

        auth_store.complete_reset(auth_store.reset_for_token(mine))

        self.assertIsNotNone(auth_store.reset_for_token(theirs))

    def test_an_unknown_reset_link_resolves_to_nothing(self):
        self.assertIsNone(auth_store.reset_for_token("made-up"))

    # -- carried-over passwords ------------------------------------------------------

    def test_a_legacy_hash_is_kept_for_the_migration_trigger_and_can_be_forgotten(self):
        auth_store.remember_legacy_password(
            email="a@example.com", password_hash="pbkdf2_sha256$...",
            name="A", phone="1", now=self.now)

        record = auth_store.legacy_password("a@example.com")
        self.assertEqual(record.password_hash, "pbkdf2_sha256$...")
        self.assertGreater(record.ttl, int(self.now.timestamp()))

        auth_store.forget_legacy_password("a@example.com")
        self.assertIsNone(auth_store.legacy_password("a@example.com"))
