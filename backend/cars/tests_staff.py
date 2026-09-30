"""The server-rendered staff pages: the masthead, and who may see what on it.

The queue pages themselves are tested beside the features they serve --
`tests_requests.py` for leads, `tests_staff_cars.py` for inventory. What is here is
the frame around them, which no single feature owns.
"""

import json
import re
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from .store import cars as car_store
from .store import requests as request_store
from .tests import DynamoReset, make_manager
from . import cognito
from .staff import context
from .staff.permissions import may
from . import tests_fake_cognito as fake_cognito
from .tests_fake_cognito import FakeCognito, sign_in


def make_staff(username="staffer"):
    """An inventory manager, which is what the shop's staff actually are.

    The groups are the real ones `permissions.py` reads, so these tests exercise the
    actual permission set rather than an invented one.
    """
    return make_manager(username=username)


def make_owner(username="owner"):
    """An owner: everything, including what OWNER_ONLY holds back from a manager."""
    user = fake_cognito.make_user(f"{username}@example.com", name="The Owner",
                                  groups=(cognito.STAFF_GROUP, cognito.OWNERS_GROUP))
    return user, "owner-pw-123456"


def make_staff_without_permissions(username="newstarter"):
    """In `staff`, and in no other group.

    The door and the rooms are separate: `staff` is what gets somebody past
    `staff_required`, and a group is what decides which pages they may then read. This
    is a new starter nobody has given a role to yet.
    """
    return fake_cognito.make_user(f"{username}@example.com", name="New Starter",
                                  groups=(cognito.STAFF_GROUP,))


class MastheadCountTests(FakeCognito, DynamoReset, SimpleTestCase):
    """The number on the tab is the same number the page shows, on every page."""

    def setUp(self):
        super().setUp()
        self.staff, _ = make_manager()
        sign_in(self.client, self.staff, staff=True)

    def nav(self, route="staff:car-list"):
        html = self.client.get(reverse(route)).content.decode()
        return html.split("<nav>")[1].split("</nav>")[0]

    def test_nothing_waiting_means_no_numbers(self):
        self.assertNotIn("nav__count", self.nav())

    def test_the_tab_counts_the_backlog_from_any_page(self):
        """Leads are the only thing left that waits for somebody. It was three counts
        -- questions and test drives went with their features."""
        for n in range(2):
            request_store.create(name=f"Hana {n}", email="h@example.com", phone="1",
                                 details="A Tanto")

        for route in ("staff:car-list", "staff:request-list"):
            self.assertIn('Requests <span class="nav__count">2', self.nav(route))

    def test_dealing_with_it_takes_the_number_off_the_tab(self):
        wish = request_store.create(name="Hana", email="h@example.com", phone="1",
                                    details="A Tanto")
        self.assertIn('Requests <span class="nav__count">1', self.nav())

        request_store.resolve(wish, staff_sub="x")

        self.assertNotIn("nav__count", self.nav())


class MastheadShowsOnlyWhatOpensTests(FakeCognito, DynamoReset, SimpleTestCase):
    """The menu used to offer all eight sections to everybody.

    The Staff link was the visible half -- `staff.*` is OWNER_ONLY, so a manager
    following it got a 403. Hiding a link is not the control and is not tested as one:
    the `@requires` guards are asserted elsewhere and still hold. What is asserted here
    is that the menu agrees with them.
    """

    def nav_links(self, user):
        """The hrefs the masthead offers this person, from a page they can load."""
        sign_in(self.client, user, staff=True)
        response = self.client.get(reverse("staff:index"), follow=True)
        html = response.content.decode()
        if "<nav>" not in html:
            return []
        nav = html.split("<nav>")[1].split("</nav>")[0]
        return re.findall(r'href="([^"]+)"', nav)

    def test_a_manager_is_not_offered_the_staff_page(self):
        manager, _ = make_staff()

        links = self.nav_links(manager)

        self.assertNotIn(reverse("staff:staff-list"), links)
        # And still has the sections that are theirs.
        for route in ("staff:car-list", "staff:request-list"):
            self.assertIn(reverse(route), links)

    def test_an_owner_is_offered_the_staff_page(self):
        owner, _ = make_owner()

        self.assertIn(reverse("staff:staff-list"), self.nav_links(owner))

    def test_a_new_starter_with_no_role_is_offered_nothing(self):
        """Asserted on the processor rather than on a page, because there is no page.

        Every template carrying the masthead is behind `@requires`, and this account may
        open none of them -- `staff:index` answers it with a bare sentence and no
        masthead at all. So the menu it would be shown is only reachable here. It is
        still worth pinning: the day somebody gives this group one action, the menu has
        to be right, and it will be for the same reason.
        """
        starter = make_staff_without_permissions()
        request = mock.Mock(staff=starter)

        offered = context.can(request)["can"]

        self.assertEqual(sorted(name for name, ok in offered.items() if ok), [])
        self.assertEqual(self.nav_links(starter), [])

    def test_the_menu_and_the_guards_read_the_same_table(self):
        """The invariant NAV_ACTIONS exists for: every item names an action the
        permission layer actually knows, so a link can never be guarded by a typo."""
        for name, action in context.NAV_ACTIONS.items():
            with self.subTest(item=name):
                self.assertTrue(may({cognito.OWNERS_GROUP}, action),
                                f"{action!r} is not an action an owner may take")
