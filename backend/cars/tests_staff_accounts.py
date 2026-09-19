"""Staff accounts and the customer list.

Carries over what `StaffAdministrationTests` was protecting. That class spent 207 lines
proving an inventory manager could administer colleagues *without* being able to escalate
-- a filtered queryset, a narrowed fieldset, a `save_model` forcing `is_superuser` false,
and a permission hook refusing an owner target by direct URL: four guards that all had to
agree with each other.

`staff.*` is in `OWNER_ONLY` now, so a manager cannot open these pages at all. Most of
those tests therefore have nothing left to assert -- the escalation is impossible because
the door is shut, not because four guards hold it. What is asserted here is the shut
door, plus the parts that still bite somebody who *does* have the power: an owner can
lock themselves out, and the group list is the only thing between a form post and
`owners`.
"""

from django.urls import reverse

from . import cognito
from .staff import auth as staff_auth
from .staff.forms import StaffAccountForm
from .staff.permissions import ASSIGNABLE_GROUPS
from .tests_staff_auth import StaffAuthTestCase


class StaffAccountTestCase(StaffAuthTestCase):
    def sign_in_as_owner(self, email="owner@example.com"):
        token = self.staff_token(email=email,
                                 groups=(cognito.STAFF_GROUP, cognito.OWNERS_GROUP))
        self.client.cookies[staff_auth.STAFF_COOKIE] = token
        return cognito.user_for(email)

    def sign_in_as_manager(self, email="manager@example.com"):
        token = self.staff_token(email=email,
                                 groups=(cognito.STAFF_GROUP,
                                         cognito.INVENTORY_GROUP))
        self.client.cookies[staff_auth.STAFF_COOKIE] = token
        return cognito.user_for(email)

    def make_colleague(self, email="colleague@example.com", name="A Colleague"):
        cognito.create_staff(email=email, name=name,
                             temporary_password="Temp-pw-123456")
        return cognito.user_for(email)


class ManagerIsLockedOutTests(StaffAccountTestCase):
    """The replacement for the entire StaffAccountAdmin sandbox.

    Every escalation that page defended against -- opening the owner's account, granting
    yourself a permission, joining a group you should not -- needed the page to be
    reachable first. It is not.
    """

    def test_a_manager_cannot_list_staff(self):
        self.sign_in_as_manager()

        self.assertEqual(self.client.get(reverse("staff:staff-list")).status_code, 403)

    def test_a_manager_cannot_open_the_add_form(self):
        self.sign_in_as_manager()

        self.assertEqual(self.client.get(reverse("staff:staff-add")).status_code, 403)

    def test_a_manager_cannot_reach_an_account_by_direct_url(self):
        """The old `has_change_permission` hook, in the form it now takes.

        Filtering a list was never enough by itself; a guessed identifier had to be
        refused too. Here the refusal is the same for every identifier, because the
        action is denied before anything is looked up.
        """
        colleague = self.make_colleague()
        self.sign_in_as_manager()

        response = self.client.get(
            reverse("staff:staff-edit", args=[colleague.sub]))

        self.assertEqual(response.status_code, 403)

    def test_a_manager_may_still_read_the_customer_list(self):
        """`customer.view` is theirs; `staff.view` is not. The split is the point."""
        self.sign_in_as_manager()

        self.assertEqual(
            self.client.get(reverse("staff:customer-list")).status_code, 200)


class StaffIndexTests(StaffAccountTestCase):
    """`/api/staff/` had no route, and it is where sign-in lands by default."""

    def test_an_owner_lands_on_the_car_list(self):
        self.sign_in_as_owner()

        response = self.client.get(reverse("staff:index"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("staff:car-list"))

    def test_a_manager_lands_on_the_car_list_too(self):
        self.sign_in_as_manager()

        response = self.client.get(reverse("staff:index"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("staff:car-list"))

    def test_somebody_with_no_role_is_told_so_rather_than_403d(self):
        """A 403 here is indistinguishable from a failed login, which is the thing the
        person is most likely to assume."""
        token = self.staff_token(email="newstarter@example.com",
                                 groups=(cognito.STAFF_GROUP,))
        self.client.cookies[staff_auth.STAFF_COOKIE] = token

        response = self.client.get(reverse("staff:index"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"no role yet", response.content)

    def test_a_guest_is_sent_to_sign_in(self):
        response = self.client.get(reverse("staff:index"))

        self.assertEqual(response.status_code, 302)


class RosterTests(StaffAccountTestCase):
    def test_an_owner_sees_the_roster(self):
        self.make_colleague()
        self.sign_in_as_owner()

        body = self.client.get(reverse("staff:staff-list")).content.decode()

        self.assertIn("colleague@example.com", body)
        self.assertIn("owner@example.com", body)

    def test_an_owner_is_shown_as_an_owner(self):
        """The old page hid superusers from managers. A manager cannot see this page at
        all, so the owner is shown rather than hidden -- an owner needs to know who else
        holds the keys.
        """
        self.sign_in_as_owner()

        body = self.client.get(reverse("staff:staff-list")).content.decode()

        self.assertIn("Owner", body)

    def test_a_customer_token_reaches_nothing(self):
        token = self.staff_token(email="both@example.com",
                                 groups=(cognito.STAFF_GROUP, cognito.OWNERS_GROUP),
                                 client="customer")
        self.client.cookies[staff_auth.STAFF_COOKIE] = token

        self.assertEqual(self.client.get(reverse("staff:staff-list")).status_code, 302)


class SelfLockoutTests(StaffAccountTestCase):
    """One careless tick would otherwise cost an owner the account, with no way back
    short of the AWS console."""

    def test_an_owner_cannot_deactivate_their_own_account(self):
        owner = self.sign_in_as_owner()

        response = self.client.post(
            reverse("staff:staff-edit", args=[owner.sub]),
            {"email": owner.email, "name": "The Owner", "groups": []},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You cannot deactivate your own account.")

    def test_the_account_really_is_still_active_afterwards(self):
        owner = self.sign_in_as_owner()

        self.client.post(
            reverse("staff:staff-edit", args=[owner.sub]),
            {"email": owner.email, "name": "The Owner", "groups": []},
        )

        self.assertTrue(
            cognito.client().admin_get_user(
                UserPoolId=cognito._pool(), Username=owner.sub)["Enabled"])

    def test_deactivating_somebody_else_is_allowed(self):
        colleague = self.make_colleague()
        self.sign_in_as_owner()

        response = self.client.post(
            reverse("staff:staff-edit", args=[colleague.sub]),
            {"email": colleague.email, "name": "A Colleague", "groups": []},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            cognito.client().admin_get_user(
                UserPoolId=cognito._pool(), Username=colleague.sub)["Enabled"])


class EscalationTests(StaffAccountTestCase):
    """`owners` is not on the form, and a POST cannot put it there."""

    def test_the_form_offers_only_the_assignable_groups(self):
        choices = [value for value, _label
                   in StaffAccountForm().fields["groups"].choices]

        self.assertEqual(tuple(choices), ASSIGNABLE_GROUPS)
        self.assertNotIn(cognito.OWNERS_GROUP, choices)

    def test_posting_the_owners_group_is_refused_rather_than_ignored(self):
        """A value outside the choices fails validation. Silently dropping it would
        leave the form looking as though it had succeeded."""
        colleague = self.make_colleague()
        self.sign_in_as_owner()

        response = self.client.post(
            reverse("staff:staff-edit", args=[colleague.sub]),
            {"email": colleague.email, "name": "A Colleague",
             "groups": [cognito.OWNERS_GROUP], "is_active": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(cognito.OWNERS_GROUP, cognito.groups_of(colleague.sub))

    def test_granting_the_inventory_role_works(self):
        colleague = self.make_colleague()
        self.sign_in_as_owner()

        self.client.post(
            reverse("staff:staff-edit", args=[colleague.sub]),
            {"email": colleague.email, "name": "A Colleague",
             "groups": [cognito.INVENTORY_GROUP], "is_active": "on"},
        )

        self.assertIn(cognito.INVENTORY_GROUP, cognito.groups_of(colleague.sub))


class CreationTests(StaffAccountTestCase):
    def test_creating_an_account_puts_them_in_the_staff_group(self):
        self.sign_in_as_owner()

        self.client.post(reverse("staff:staff-add"), {
            "email": "newbie@example.com", "name": "New Starter",
            "groups": [cognito.INVENTORY_GROUP], "is_active": "on",
        })

        groups = cognito.groups_of("newbie@example.com")
        self.assertIn(cognito.STAFF_GROUP, groups)
        self.assertIn(cognito.INVENTORY_GROUP, groups)

    def test_the_first_password_is_shown_once_because_nothing_emails_it(self):
        """The pool sends no mail, so this message is the only delivery there is."""
        self.sign_in_as_owner()

        response = self.client.post(reverse("staff:staff-add"), {
            "email": "newbie@example.com", "name": "New Starter",
            "groups": [], "is_active": "on",
        }, follow=True)

        body = response.content.decode()
        self.assertIn("Their first password is", body)
        self.assertIn("it is not shown again", body)

    def test_a_duplicate_address_is_refused_in_words(self):
        self.make_colleague()
        self.sign_in_as_owner()

        response = self.client.post(reverse("staff:staff-add"), {
            "email": "colleague@example.com", "name": "Again", "groups": [],
            "is_active": "on",
        })

        self.assertContains(response, "Somebody already has that address.")


class ThereIsNoDeleteTests(StaffAccountTestCase):
    def test_no_route_deletes_a_staff_account(self):
        """Deactivate, never delete. The Django page refused deletion because a removed
        account orphans every booking and question naming that person, and items keyed
        on a sub make that more true rather than less.
        """
        from .staff import urls as staff_urls

        names = {p.name for p in staff_urls.urlpatterns}

        self.assertNotIn("staff-delete", names)
