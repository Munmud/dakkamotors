"""Stopping a car's advertisement when it gets its test drive.

Meta is never actually called: `meta_ads.pause_ad_set` is patched, which is the whole
seam. It is a five-line urllib POST, and a fake Graph API would test urllib rather than
anything this project decides. What these tests are about is the decisions -- once and
only once, never at a booking's expense, and an honest message when Meta says no.
"""

from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import advertising
from . import booking as booking_rules
from . import meta_ads
from .store import cars as car_store
from .tests import DynamoReset, future_slot, make_booking, make_car
from .tests_fake_cognito import FakeCognito, make_user

AD_SET = "1234567890123"

#: The owner's address. Blank by default, and `notify_staff_of_paused_ad` returns early
#: without one -- so every class here that asserts on the message has to set it.
OWNER = override_settings(STAFF_ALERT_EMAIL="owner@example.com")


def booking_for(car, *, user=None, slot=None):
    """A live booking for a car, written straight to the store."""
    return make_booking(user or make_user("hana@example.com", name="Hana Sato"),
                        slot or future_slot(), car=car)


@OWNER
class AdPauseTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(meta_ads, "pause_ad_set", return_value=True)
        self.pause = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_booking_pauses_the_cars_ad_set_and_tells_the_owner(self):
        car = make_car("AD-1", ad_set_id=AD_SET)

        with mock.patch("cars.mail.queue_email", return_value=True) as queued:
            self.assertTrue(advertising.stop_for(booking_for(car)))

        self.pause.assert_called_once_with(AD_SET)
        self.assertEqual(queued.call_count, 1)
        subject = queued.call_args.kwargs["subject"]
        self.assertIn("Ad stopped", subject)
        self.assertIn("Daihatsu Tanto", subject)

    def test_the_second_booking_neither_pauses_nor_emails_again(self):
        """Two people booking the same car in the same second is what an advertisement
        is for. Only one of them stopped it, and only one message should say so."""
        car = make_car("AD-2", ad_set_id=AD_SET)
        first = booking_for(car, user=make_user("a@example.com"))
        second = booking_for(car, user=make_user("b@example.com"))

        with mock.patch("cars.mail.queue_email", return_value=True) as queued:
            self.assertTrue(advertising.stop_for(first))
            self.assertFalse(advertising.stop_for(second))

        self.pause.assert_called_once_with(AD_SET)
        self.assertEqual(queued.call_count, 1)

    def test_a_car_with_no_ad_set_does_not_call_meta_at_all(self):
        car = make_car("AD-3")

        with mock.patch("cars.mail.queue_email", return_value=True) as queued:
            self.assertFalse(advertising.stop_for(booking_for(car)))

        self.pause.assert_not_called()
        queued.assert_not_called()

    def test_a_booking_with_no_car_is_left_alone(self):
        """Every guard here keys on the car; a booking with none has nothing to stop."""
        booking = make_booking(make_user("c@example.com"), future_slot())

        self.assertFalse(advertising.stop_for(booking))
        self.pause.assert_not_called()

    def test_the_pause_is_stamped_on_the_car(self):
        car = make_car("AD-4", ad_set_id=AD_SET)

        with mock.patch("cars.mail.queue_email", return_value=True):
            advertising.stop_for(booking_for(car))

        self.assertIsNotNone(car_store.get(car.car_id).ad_paused_at)


@OWNER
class AdFailureTests(FakeCognito, DynamoReset, SimpleTestCase):
    """What happens when Meta will not play. None of it may reach the customer."""

    def test_a_meta_outage_does_not_fail_the_booking(self):
        car = make_car("AD-5", ad_set_id=AD_SET)
        slot = future_slot()
        user = make_user("hana@example.com", name="Hana Sato")

        with mock.patch.object(meta_ads, "pause_ad_set",
                               side_effect=meta_ads.MetaError("down")), \
                mock.patch("cars.mail.queue_email", return_value=True):
            booking = booking_rules.create_booking(user=user, slot_id=slot.slot_id,
                                                   car=car)

        self.assertIsNotNone(booking.booking_id)
        self.assertTrue(booking.is_active)

    def test_meta_refusing_says_so_rather_than_claiming_the_ad_stopped(self):
        """The most expensive sentence on the site would be "stopped" when it is not.
        The ad set is still spending and only a person can stop it now."""
        car = make_car("AD-6", ad_set_id=AD_SET)

        with mock.patch.object(meta_ads, "pause_ad_set",
                               side_effect=meta_ads.MetaError("down")), \
                mock.patch("cars.mail.queue_email", return_value=True) as queued:
            self.assertTrue(advertising.stop_for(booking_for(car)))

        sent = queued.call_args.kwargs
        self.assertIn("Pause this ad by hand", sent["subject"])
        self.assertIn("still spending", sent["text"].lower())
        self.assertIn(AD_SET, sent["text"])
        self.assertNotIn("has been paused", sent["text"])

    def test_anything_else_going_wrong_is_swallowed_too(self):
        """`stop_for` is the boundary: a booking must never fail over advertising, and
        the only way to be sure is that nothing at all escapes it."""
        car = make_car("AD-7", ad_set_id=AD_SET)

        with mock.patch.object(car_store, "claim_ad_pause",
                               side_effect=RuntimeError("boom")):
            self.assertFalse(advertising.stop_for(booking_for(car)))


@OWNER
class AdSetLifetimeTests(FakeCognito, DynamoReset, SimpleTestCase):
    def test_changing_the_ad_set_clears_the_pause_so_a_new_campaign_can_stop(self):
        """Otherwise a second campaign for the same car could never stop itself --
        silent, and visible only as a bill."""
        car = make_car("AD-8", ad_set_id=AD_SET)
        with mock.patch.object(meta_ads, "pause_ad_set", return_value=True), \
                mock.patch("cars.mail.queue_email", return_value=True):
            advertising.stop_for(booking_for(car))

        car_store.update(car_store.get(car.car_id), now=timezone.now(),
                         ad_set_id="9999999999999")

        self.assertIsNone(car_store.get(car.car_id).ad_paused_at)

    def test_clearing_the_ad_set_clears_the_pause_too(self):
        car = make_car("AD-9", ad_set_id=AD_SET)
        with mock.patch.object(meta_ads, "pause_ad_set", return_value=True), \
                mock.patch("cars.mail.queue_email", return_value=True):
            advertising.stop_for(booking_for(car))

        car_store.update(car_store.get(car.car_id), now=timezone.now(), ad_set_id=None)

        fresh = car_store.get(car.car_id)
        self.assertIsNone(fresh.ad_set_id)
        self.assertIsNone(fresh.ad_paused_at)

    def test_an_unrelated_edit_leaves_the_pause_alone(self):
        """Only a change to the id resets it. A price edit is not a new campaign."""
        car = make_car("AD-10", ad_set_id=AD_SET)
        with mock.patch.object(meta_ads, "pause_ad_set", return_value=True), \
                mock.patch("cars.mail.queue_email", return_value=True):
            advertising.stop_for(booking_for(car))

        car_store.update(car_store.get(car.car_id), now=timezone.now(),
                         price_jpy=640000)

        self.assertIsNotNone(car_store.get(car.car_id).ad_paused_at)


class MetaClientTests(SimpleTestCase):
    """The urllib call itself, at the one boundary worth asserting: is it configured."""

    def test_no_token_means_no_call(self):
        """Every environment but production. It is what keeps this suite, and local
        development, from reaching Meta at all."""
        with mock.patch("urllib.request.urlopen") as opened:
            self.assertFalse(meta_ads.pause_ad_set(AD_SET))
        opened.assert_not_called()

    @override_settings(META_ADS_TOKEN="sys-user-token")
    def test_it_posts_the_pause_to_the_ad_set(self):
        with mock.patch("urllib.request.urlopen") as opened:
            opened.return_value.__enter__.return_value.read.return_value = b"{}"
            self.assertTrue(meta_ads.pause_ad_set(AD_SET))

        request = opened.call_args[0][0]
        self.assertEqual(request.method, "POST")
        self.assertIn(f"/{meta_ads.GRAPH_VERSION}/{AD_SET}", request.full_url)
        body = request.data.decode()
        self.assertIn("status=PAUSED", body)
        # The token goes in the body, never the URL: a URL is what turns up in logs.
        self.assertNotIn("token", request.full_url)
        self.assertIn("access_token=sys-user-token", body)

    @override_settings(META_ADS_TOKEN="sys-user-token")
    def test_an_unreachable_meta_raises_the_typed_error(self):
        import urllib.error

        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("no route")):
            with self.assertRaises(meta_ads.MetaError):
                meta_ads.pause_ad_set(AD_SET)


class AdSetPrivacyTests(DynamoReset, SimpleTestCase):
    def test_the_ad_set_id_never_reaches_a_visitor(self):
        """It is not a secret in the way a token is, but it is the shop's commercial
        business and it has no reason to be in a public payload. The serializers list
        their fields explicitly, so this holds by construction -- and this test is what
        would notice if one ever grew a `__all__`."""
        car = make_car("AD-PRIV", ad_set_id=AD_SET)

        for path in ("/api/cars/", f"/api/cars/{car.slug}/"):
            with self.subTest(path=path):
                self.assertNotIn(AD_SET, self.client.get(path).content.decode())

        with mock.patch("cars.pages.asset_tags", return_value=""):
            page = self.client.get(car.get_absolute_url()).content.decode()
        self.assertNotIn(AD_SET, page)
