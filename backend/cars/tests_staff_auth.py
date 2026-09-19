"""Staff authentication on Cognito.

The hosted UI's token exchange is the one part no local stand-in covers -- moto does not
implement `/oauth2/token` -- so it is isolated in `exchange_code` and stubbed here.
Everything around it is real: tokens minted by moto and verified for real, the client_id
check, the signed `state`, the group map, and the cookie.

The client_id check is the piece most worth pinning down. A staff member can sign in
through the customer endpoint like anybody else and be handed a token whose
`cognito:groups` contains `staff`. If these pages accepted it, the customer sign-in form
would be a way into the staff pages -- bypassing the hosted UI and its MFA entirely.
"""

from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from . import cognito
from .staff import auth as staff_auth
from .staff.permissions import ASSIGNABLE_GROUPS, GROUP_ACTIONS, may
from .tests import DynamoReset, MAIL_SETTINGS
from .tests_cognito import CognitoBackend


@override_settings(**MAIL_SETTINGS)
class StaffAuthTestCase(CognitoBackend, DynamoReset, SimpleTestCase):
    def staff_token(self, email="staffer@example.com", groups=("staff",),
                    client="staff"):
        """A real, signed token for somebody in the given groups."""
        password = "staff-pw-123456"
        cognito.sign_up(email=email, password=password, name="Staff Member")
        cognito.confirm(email)
        for group in groups:
            cognito.add_to_group(email, group)

        import boto3

        raw = boto3.client("cognito-idp", region_name="ap-northeast-1",
                           endpoint_url=self.endpoint,
                           aws_access_key_id="local", aws_secret_access_key="local")
        client_id = (self.staff_client if client == "staff" else self.customer_client)
        result = raw.admin_initiate_auth(
            UserPoolId=self.pool_id, ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )["AuthenticationResult"]
        return result["AccessToken"]


class StateSigningTests(StaffAuthTestCase):
    """`state` carries the destination through Cognito and back, signed."""

    def test_a_destination_survives_the_round_trip(self):
        state = staff_auth.sign_state("/api/staff/cars/")
        self.assertEqual(staff_auth.read_state(state), "/api/staff/cars/")

    def test_a_tampered_state_carries_nothing(self):
        state = staff_auth.sign_state("/api/staff/cars/")
        payload, _, _mac = state.partition(".")
        forged = payload + "." + ("0" * 32)

        self.assertEqual(staff_auth.read_state(forged), "")

    def test_a_state_signed_with_another_key_carries_nothing(self):
        with override_settings(SECRET_KEY="a-different-secret"):
            state = staff_auth.sign_state("/api/staff/cars/")
        self.assertEqual(staff_auth.read_state(state), "")

    def test_an_offsite_destination_is_discarded(self):
        for hostile in ("//evil.com", "https://evil.com", "javascript:alert(1)"):
            with self.subTest(hostile=hostile):
                self.assertEqual(
                    staff_auth.read_state(staff_auth.sign_state(hostile)), "")

    def test_rubbish_carries_nothing(self):
        for value in ("", "no-dot", "a.b", None):
            with self.subTest(value=value):
                self.assertEqual(staff_auth.read_state(value), "")


class StaffCookieTests(StaffAuthTestCase):
    def test_an_inventory_manager_can_open_the_pages(self):
        token = self.staff_token(groups=("staff", "inventory-managers"))
        self.client.cookies[staff_auth.STAFF_COOKIE] = token

        response = self.client.get(reverse("staff:car-list"))

        self.assertEqual(response.status_code, 200)

    def test_being_staff_alone_opens_nothing(self):
        """The door and the rooms are separate.

        `staff` is what gets somebody past `staff_required`; a group is what decides
        which pages they may then read. A new starter in no group sees nothing, which
        is what the admin index used to show them.
        """
        self.client.cookies[staff_auth.STAFF_COOKIE] = self.staff_token()

        response = self.client.get(reverse("staff:car-list"))

        self.assertEqual(response.status_code, 403)

    def test_a_customer_token_is_refused_even_for_a_staff_member(self):
        """The whole staff/customer isolation, in one string comparison.

        The same person, in the `staff` group, with a genuine signed token -- but minted
        by the customer app client. Accepting it would make the customer sign-in form a
        way past the hosted UI and its MFA.
        """
        token = self.staff_token(groups=("staff", "inventory-managers"),
                                 client="customer")
        self.client.cookies[staff_auth.STAFF_COOKIE] = token

        response = self.client.get(reverse("staff:car-list"))

        self.assertEqual(response.status_code, 302)

    def test_somebody_not_in_the_staff_group_is_refused(self):
        token = self.staff_token(email="buyer@example.com", groups=())
        self.client.cookies[staff_auth.STAFF_COOKIE] = token

        response = self.client.get(reverse("staff:car-list"))

        self.assertEqual(response.status_code, 302)

    def test_an_unreadable_cookie_is_simply_not_signed_in(self):
        self.client.cookies[staff_auth.STAFF_COOKIE] = "not-a-token"

        response = self.client.get(reverse("staff:car-list"))

        self.assertEqual(response.status_code, 302)

    def test_signing_out_clears_the_cookie(self):
        self.client.cookies[staff_auth.STAFF_COOKIE] = self.staff_token()

        response = self.client.get(reverse("staff:sign-out"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.cookies[staff_auth.STAFF_COOKIE].value, "")


class CallbackTests(StaffAuthTestCase):
    """The hosted-UI return leg, with only the token exchange stubbed."""

    def callback(self, tokens, state=None):
        with mock.patch("cars.staff.views_auth.exchange_code", return_value=tokens):
            return self.client.get(
                reverse("staff:auth-callback"),
                {"code": "an-authorization-code",
                 "state": state or staff_auth.sign_state("/api/staff/cars/")})

    def test_the_exchange_uses_the_registered_redirect_uri(self):
        """OAuth2 requires it byte-identical to the authorize call; this shipped wrong.

        Cognito calls the callback without a trailing slash. The view used to derive the
        value from `request.build_absolute_uri(request.path)`, so if anything rewrote the
        path -- APPEND_SLASH did -- the exchange sent the rewritten one, Cognito answered
        400, and the callback bounced the browser back to sign in. The browser looped
        until it gave up: "redirected you too many times", with nothing in the UI naming
        a redirect_uri.
        """
        seen = {}

        def capture(code, uri):
            seen["uri"] = uri
            return {"access_token": self.staff_token(), "expires_in": 1800}

        with mock.patch("cars.staff.views_auth.exchange_code", side_effect=capture):
            self.client.get(reverse("staff:auth-callback"),
                            {"code": "an-authorization-code",
                             "state": staff_auth.sign_state("/api/staff/cars/")})

        self.assertEqual(seen["uri"], staff_auth.redirect_uri())
        self.assertFalse(staff_auth.redirect_uri().endswith("/"))

        # And the authorize leg must send the very same string.
        import urllib.parse

        with override_settings(COGNITO_DOMAIN="example.auth.ap-northeast-1.amazoncognito.com"):
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(staff_auth.sign_in_url()).query)
        self.assertEqual(query["redirect_uri"], [staff_auth.redirect_uri()])

    def test_the_callback_route_needs_no_append_slash_redirect(self):
        """Cognito is registered against the slashless path; serving only the slashed one
        made Django 301 the authorization code through an extra hop."""
        response = self.client.get("/api/staff/auth/callback", {"code": ""})

        self.assertNotEqual(response.status_code, 301)

    def test_a_staff_token_sets_the_cookie_and_goes_where_state_said(self):
        response = self.callback({"access_token": self.staff_token(),
                                  "expires_in": 1800})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/api/staff/cars/")
        self.assertTrue(response.cookies[staff_auth.STAFF_COOKIE].value)
        self.assertTrue(response.cookies[staff_auth.STAFF_COOKIE]["httponly"])

    def test_a_non_staff_token_is_refused_rather_than_redirected(self):
        response = self.callback({"access_token":
                                  self.staff_token(email="buyer@example.com",
                                                   groups=()),
                                  "expires_in": 1800})

        self.assertEqual(response.status_code, 403)
        self.assertNotIn(staff_auth.STAFF_COOKIE, response.cookies)

    def test_a_tampered_state_falls_back_to_the_staff_index(self):
        response = self.callback({"access_token": self.staff_token(),
                                  "expires_in": 1800},
                                 state="forged.0000")

        self.assertEqual(response["Location"], "/api/staff/")

    def test_arriving_with_no_code_goes_back_to_sign_in(self):
        response = self.client.get(reverse("staff:auth-callback"))
        self.assertEqual(response.status_code, 302)

    def test_a_failed_exchange_goes_back_to_sign_in_rather_than_erroring(self):
        with mock.patch("cars.staff.views_auth.exchange_code",
                        side_effect=RuntimeError("bad code")):
            response = self.client.get(reverse("staff:auth-callback"),
                                       {"code": "reused"})

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(staff_auth.STAFF_COOKIE, response.cookies)

    def test_a_forged_token_is_refused(self):
        response = self.callback({"access_token": "not.a.token", "expires_in": 1800})

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(staff_auth.STAFF_COOKIE, response.cookies)


class PermissionTests(SimpleTestCase):
    """Groups replace Django's permission rows."""

    def test_an_owner_may_do_anything(self):
        owner = {cognito.OWNERS_GROUP}
        for action in ("car.delete", "staff.change", "booking.change", "anything.new"):
            with self.subTest(action=action):
                self.assertTrue(may(owner, action))

    def test_an_inventory_manager_may_run_the_shop(self):
        manager = {cognito.INVENTORY_GROUP}
        for action in ("car.add", "car.change", "car.delete", "question.change",
                       "booking.change", "slot.change", "schedule.change"):
            with self.subTest(action=action):
                self.assertTrue(may(manager, action))

    def test_an_inventory_manager_may_not_touch_staff_accounts(self):
        """The privilege escalation StaffAccountAdmin's sandbox existed to prevent.

        Whoever can edit staff accounts can open the owner's, reset its password, or add
        themselves to `owners`. That is what the permission means, so it is an owner's
        alone -- and it is refused even though `owners` is not in the group set.
        """
        manager = {cognito.INVENTORY_GROUP}
        for action in ("staff.view", "staff.add", "staff.change", "staff.delete",
                       "group.change"):
            with self.subTest(action=action):
                self.assertFalse(may(manager, action))

    def test_being_in_no_group_grants_nothing(self):
        self.assertFalse(may(set(), "car.view"))

    def test_the_action_list_never_mentions_authentication(self):
        """`FORBIDDEN_APP_LABELS` as a unit test.

        The old group was reconciled on every deploy with a guard that it never held an
        `auth` permission. There is no Django permission left to hold, but the intent
        survives: nothing outside `owners` may reach identity.
        """
        for group, actions in GROUP_ACTIONS.items():
            if group == cognito.OWNERS_GROUP:
                continue
            with self.subTest(group=group):
                self.assertFalse({a for a in actions if a.startswith(("staff.",
                                                                     "group."))})

    def test_only_the_inventory_group_can_be_handed_out(self):
        """Anything more powerful added later is invisible here, so it cannot be
        given away by somebody who should not have it."""
        self.assertEqual(ASSIGNABLE_GROUPS, (cognito.INVENTORY_GROUP,))
        self.assertNotIn(cognito.OWNERS_GROUP, ASSIGNABLE_GROUPS)


class SignInUrlTests(StaffAuthTestCase):
    def test_without_a_hosted_domain_there_is_nowhere_to_send_them(self):
        with override_settings(COGNITO_DOMAIN=""):
            url = staff_auth.sign_in_url("/api/staff/cars/")
        self.assertIn("/api/staff/auth/not-configured", url)

    def test_with_a_domain_it_points_at_the_hosted_ui(self):
        with override_settings(COGNITO_DOMAIN="dakkamotors-staff.auth.example.com",
                               ALLOWED_HOSTS=["dakkamotors.com"]):
            url = staff_auth.sign_in_url("/api/staff/cars/")

        self.assertIn("dakkamotors-staff.auth.example.com/oauth2/authorize", url)
        self.assertIn("response_type=code", url)
        self.assertIn(f"client_id={self.staff_client}", url)
        self.assertIn("state=", url)
