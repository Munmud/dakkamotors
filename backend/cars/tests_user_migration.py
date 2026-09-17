"""The Cognito UserMigration trigger.

This is the piece that decides whether the cutover is silent or whether every existing
customer gets an unprompted "your password no longer works" email. It has to accept
exactly what Django wrote, so it is tested against hashes Django actually produces.

The code under test is **extracted from `infra/data.yaml`** rather than copied here. A
copy would drift, and the whole value of the test is that the thing which will run in
production is the thing that was checked.
"""

import ast
import io
import pathlib
import types

from django.contrib.auth.hashers import make_password
from django.test import SimpleTestCase, override_settings

TEMPLATE = (pathlib.Path(__file__).resolve().parents[2]
            / "infra" / "data.yaml")


def load_trigger():
    """Pull the inline Lambda source out of the CloudFormation template and import it.

    boto3 is stubbed: the verifier is pure stdlib, and the handler's DynamoDB calls are
    not what these tests are about.
    """
    raw = io.open(TEMPLATE, encoding="utf-8").read()
    marker = "        ZipFile: |\n"
    start = raw.index(marker) + len(marker)

    lines = []
    for line in raw[start:].split("\n"):
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line[10:] if line.startswith("          ") else line)
    source = "\n".join(lines)

    ast.parse(source)  # fail loudly rather than at deploy time

    import os
    import sys

    os.environ.setdefault("TABLE_NAME", "dakkamotors-test")

    # The module does `import boto3` at the top, so the stub has to be in sys.modules
    # before the exec rather than injected into its namespace afterwards. Restored
    # straight away so nothing else in the suite sees it.
    real = sys.modules.get("boto3")
    sys.modules["boto3"] = types.SimpleNamespace(client=lambda *a, **k: None)
    try:
        module = types.ModuleType("user_migration_trigger")
        exec(compile(source, "infra/data.yaml::UserMigrationFunction", "exec"),
             module.__dict__)
    finally:
        if real is not None:
            sys.modules["boto3"] = real
        else:
            sys.modules.pop("boto3", None)
    return module


class PasswordCarryOverTests(SimpleTestCase):
    """Django's hash format, verified by fifteen lines of stdlib."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.trigger = load_trigger()

    def django_hash(self, password):
        # The real hasher, at the real iteration count, exactly as the export would
        # have carried it across.
        with override_settings(PASSWORD_HASHERS=[
                "django.contrib.auth.hashers.PBKDF2PasswordHasher"]):
            return make_password(password)

    def test_the_right_password_is_accepted(self):
        encoded = self.django_hash("customer-pw-1234")

        self.assertTrue(self.trigger.verify("customer-pw-1234", encoded))

    def test_a_wrong_password_is_refused(self):
        encoded = self.django_hash("customer-pw-1234")

        self.assertFalse(self.trigger.verify("customer-pw-12345", encoded))
        self.assertFalse(self.trigger.verify("", encoded))

    def test_a_six_character_password_still_works(self):
        """The case the pool's MinimumLength exists for.

        Customers were held to length six, so plenty of real passwords are exactly
        that. A pool policy of eight would reject them here, at sign-in, in Cognito's
        words -- which is why the policy is six.
        """
        encoded = self.django_hash("simple")

        self.assertTrue(self.trigger.verify("simple", encoded))

    def test_a_non_ascii_password_works(self):
        """Japanese customers, Japanese keyboards."""
        encoded = self.django_hash("ひみつのことば")

        self.assertTrue(self.trigger.verify("ひみつのことば", encoded))

    def test_another_algorithm_is_refused_rather_than_guessed_at(self):
        with override_settings(PASSWORD_HASHERS=[
                "django.contrib.auth.hashers.MD5PasswordHasher"]):
            encoded = make_password("customer-pw-1234")

        self.assertFalse(self.trigger.verify("customer-pw-1234", encoded))

    def test_an_unusable_password_is_refused(self):
        """Django writes `!<random>` for an account that can never sign in."""
        from django.contrib.auth.hashers import make_password as mp

        self.assertFalse(self.trigger.verify("anything", mp(None)))

    def test_rubbish_is_refused_without_raising(self):
        for encoded in ("", "not-a-hash", "pbkdf2_sha256$only$two", "$$$"):
            with self.subTest(encoded=encoded):
                self.assertFalse(self.trigger.verify("anything", encoded))

    def test_the_iteration_count_is_read_from_the_hash_not_assumed(self):
        """Django's default climbs with every release, and the export carries whatever
        the rows were written with."""
        import hashlib
        import base64

        salt, iterations = "abcdefgh", 12345
        derived = hashlib.pbkdf2_hmac("sha256", b"known", salt.encode(), iterations)
        encoded = "pbkdf2_sha256$%d$%s$%s" % (
            iterations, salt, base64.b64encode(derived).decode())

        self.assertTrue(self.trigger.verify("known", encoded))
        self.assertFalse(self.trigger.verify("other", encoded))


class TriggerContractTests(SimpleTestCase):
    """What the trigger must hand back for Cognito to create the user silently."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.trigger = load_trigger()

    def test_an_unsupported_trigger_source_is_refused(self):
        """Forgot-password migration is not used: recovery on this pool is admin_only,
        so our own reset flow is the only one."""
        with self.assertRaises(Exception):
            self.trigger.handler(
                {"triggerSource": "UserMigration_ForgotPassword",
                 "request": {}, "response": {}}, None)

    def test_the_handler_asks_for_the_item_before_trusting_anything(self):
        """A missing carried-over account and a wrong password fail identically, so the
        sign-in form cannot be used to discover who was migrated."""
        calls = {}

        class FakeDdb:
            def get_item(self, **kwargs):
                calls["key"] = kwargs["Key"]
                return {}

            def delete_item(self, **kwargs):
                calls["deleted"] = True

        self.trigger.ddb = FakeDdb()
        event = {"triggerSource": "UserMigration_Authentication",
                 "userName": "Buyer@Example.com",
                 "request": {"password": "whatever"}, "response": {}}

        with self.assertRaises(Exception):
            self.trigger.handler(event, None)

        self.assertEqual(calls["key"]["pk"]["S"], "LEGACYPW#buyer@example.com")
        self.assertNotIn("deleted", calls)

    def test_a_good_password_returns_confirmed_attributes_and_burns_the_item(self):
        encoded = None
        with override_settings(PASSWORD_HASHERS=[
                "django.contrib.auth.hashers.PBKDF2PasswordHasher"]):
            encoded = make_password("customer-pw-1234")

        deleted = {}

        class FakeDdb:
            def get_item(self, **kwargs):
                return {"Item": {
                    "password_hash": {"S": encoded},
                    "name": {"S": "Aiko Tanaka"},
                    "phone": {"S": "080-1111-2222"},
                }}

            def delete_item(self, **kwargs):
                deleted["key"] = kwargs["Key"]

        self.trigger.ddb = FakeDdb()
        event = {"triggerSource": "UserMigration_Authentication",
                 "userName": "buyer@example.com",
                 "request": {"password": "customer-pw-1234"}, "response": {}}

        result = self.trigger.handler(event, None)

        attrs = result["response"]["userAttributes"]
        self.assertEqual(attrs["email"], "buyer@example.com")
        self.assertEqual(attrs["email_verified"], "true")
        self.assertEqual(attrs["given_name"], "Aiko")
        self.assertEqual(attrs["family_name"], "Tanaka")
        self.assertEqual(attrs["custom:phone"], "080-1111-2222")
        # CONFIRMED and SUPPRESS together are what make it silent: they just proved the
        # password, so asking them to confirm it again is the thing this avoids.
        self.assertEqual(result["response"]["finalUserStatus"], "CONFIRMED")
        self.assertEqual(result["response"]["messageAction"], "SUPPRESS")
        self.assertEqual(deleted["key"]["pk"]["S"], "LEGACYPW#buyer@example.com")
