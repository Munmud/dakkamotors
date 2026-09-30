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
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

try:
    from moto.server import ThreadedMotoServer
    MOTO = True
except Exception:  # pragma: no cover - moto is a dev-only dependency
    MOTO = False

from . import authentication, cognito
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

    def make_account(self, email="buyer@example.com", password="account-pw-1234",
                     name="Aiko Tanaka", phone="080-1111-2222"):
        """A confirmed pool user in no groups.

        It was `make_customer`, built by `cognito.sign_up` and `confirm` -- the public
        sign-up flow, which went with customer accounts. Admin-create is what remains
        and is also what actually happens now: every account in this pool is made by an
        owner, and the ones in no group are the ones nobody has given a role to yet.

        The password is set permanent, because `admin_create_user` alone leaves the
        account in FORCE_CHANGE_PASSWORD and authentication returns a challenge instead
        of the tokens these tests are about.
        """
        first, _, last = name.partition(" ")
        cognito.client().admin_create_user(
            UserPoolId=self.pool_id, Username=email, MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "given_name", "Value": first},
                {"Name": "family_name", "Value": last},
                {"Name": "custom:phone", "Value": phone},
            ],
        )
        cognito.set_password(email, password, permanent=True)
        return email, password

    def tokens_for(self, email, password, *, client=None):
        """Real RS256 tokens from the local pool.

        Straight to `admin_initiate_auth` rather than through a wrapper: `cognito.py`
        no longer has one to call, because nothing in the app signs anybody in with a
        password any more -- staff arrive through the hosted UI with a code. What these
        tests are about is the token, and this is the shortest honest way to get one.
        """
        return cognito.client().admin_initiate_auth(
            UserPoolId=self.pool_id,
            ClientId=client or self.customer_client,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )["AuthenticationResult"]


@unittest.skipUnless(MOTO, "moto is not installed; pip install -r requirements-dev.txt")
class CognitoTestCase(CognitoBackend, SimpleTestCase):
    databases = []


class TokenVerificationTests(CognitoTestCase):
    def fresh_tokens(self, **kwargs):
        return self.tokens_for(*self.make_account(**kwargs))

    def test_a_real_token_verifies_and_carries_the_subject(self):
        tokens = self.fresh_tokens()

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
        tokens = self.fresh_tokens()

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
        tokens = self.fresh_tokens()

        with self.assertRaises(Exception):
            authentication.verify(tokens["IdToken"])

    def test_a_user_built_from_claims_knows_its_groups_without_a_lookup(self):
        email, password = self.make_account()
        cognito.add_to_group(email, cognito.STAFF_GROUP)
        tokens = self.tokens_for(email, password)

        user = cognito.CognitoUser.from_claims(
            authentication.verify(tokens["AccessToken"]))

        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_authenticated)

    def test_profile_attributes_are_fetched_only_when_asked_for(self):
        email, password = self.make_account()
        tokens = self.tokens_for(email, password)
        user = cognito.CognitoUser.from_claims(
            authentication.verify(tokens["AccessToken"]))

        # Nothing loaded yet: a booking needs only the sub.
        self.assertIsNone(user._attrs)

        self.assertEqual(user.email, email)
        self.assertEqual(user.get_full_name(), "Aiko Tanaka")
        self.assertEqual(user.phone, "080-1111-2222")


class GroupTests(CognitoTestCase):
    def test_groups_replace_is_staff_and_is_superuser(self):
        email, _ = self.make_account()

        self.assertEqual(cognito.groups_of(email), [])
        cognito.add_to_group(email, cognito.STAFF_GROUP)
        cognito.add_to_group(email, cognito.INVENTORY_GROUP)

        self.assertEqual(sorted(cognito.groups_of(email)),
                         [cognito.INVENTORY_GROUP, cognito.STAFF_GROUP])

    def test_the_staff_list_is_the_group_membership(self):
        staff, _ = self.make_account(email="staffer@example.com")
        self.make_account(email="buyer@example.com")
        cognito.add_to_group(staff, cognito.STAFF_GROUP)

        members = cognito.users_in_group(cognito.STAFF_GROUP)

        self.assertEqual(len(members), 1)

    def test_removing_someone_from_staff_takes_the_flag_with_it(self):
        email, password = self.make_account()
        cognito.add_to_group(email, cognito.STAFF_GROUP)
        cognito.remove_from_group(email, cognito.STAFF_GROUP)

        tokens = self.tokens_for(email, password)
        user = cognito.CognitoUser.from_claims(
            authentication.verify(tokens["AccessToken"]))

        self.assertFalse(user.is_staff)
