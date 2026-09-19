"""Videos in the store, and the line that keeps them out of the listing card.

The interesting assertions here are the negative ones. A video shares a partition and an
`order` number space with photos, so the thing worth proving is what it *cannot* reach:
`images.for_car`, `pick_primary`, and therefore `Car.primary_image_ref`.
"""

import datetime as dt

from .store import cars, images, keys, videos
from .store.errors import NotFound
from .store.models import Car
from .tests_store import DynamoTestCase

UTC = dt.timezone.utc


def make_car(car_id, *, chassis=None):
    return Car(
        car_id=car_id,
        brand="Daihatsu",
        model_name="Tanto",
        manufacture_year=2008,
        chassis_number=chassis or f"V-{car_id}",
        status="available",
    )


class VideoStoreTests(DynamoTestCase):
    def setUp(self):
        super().setUp()
        cars.clear_slug_cache()
        self.now = dt.datetime.now(UTC)
        self.car = cars.create(make_car("1"), now=self.now)

    def test_a_video_round_trips(self):
        video = videos.create(car_id="1", video_name="cars/video/a.mp4", now=self.now)

        stored = videos.get("1", video.video_id)
        self.assertEqual(stored.video_name, "cars/video/a.mp4")
        self.assertEqual(stored.car_id, "1")
        self.assertEqual(stored.sk, keys.video_sk(video.video_id))

    def test_missing_video_raises_not_found(self):
        with self.assertRaises(NotFound):
            videos.get("1", "nope")

    def test_for_car_returns_them_in_order(self):
        videos.create(car_id="1", video_name="b.mp4", order=1, now=self.now)
        videos.create(car_id="1", video_name="a.mp4", order=0, now=self.now)

        names = [v.video_name for v in videos.for_car("1")]

        self.assertEqual(names, ["a.mp4", "b.mp4"])

    def test_deleting_removes_the_row(self):
        video = videos.create(car_id="1", video_name="a.mp4", now=self.now)

        videos.delete("1", video.video_id)

        with self.assertRaises(NotFound):
            videos.get("1", video.video_id)


class VideosCannotBecomeTheCardPhotoTests(DynamoTestCase):
    """The whole reason `CarVideo` is its own item type rather than a `kind` flag."""

    def setUp(self):
        super().setUp()
        cars.clear_slug_cache()
        self.now = dt.datetime.now(UTC)
        self.car = cars.create(make_car("1"), now=self.now)

    def test_a_video_is_invisible_to_the_photo_query(self):
        videos.create(car_id="1", video_name="a.mp4", now=self.now)

        self.assertEqual(images.for_car("1"), [])

    def test_a_video_does_not_become_the_card_photo(self):
        """Ordered first, and still not the card: `for_car` never returned it."""
        videos.create(car_id="1", video_name="a.mp4", order=0, now=self.now)
        images.create(car_id="1", image_name="cars/p.jpg", order=1, now=self.now)

        ref = Car.get(keys.car_pk("1"), keys.META).primary_image_ref

        self.assertIsNotNone(ref)
        self.assertEqual(ref["name"], "cars/p.jpg")

    def test_marking_a_video_primary_is_refused(self):
        """`set_primary` goes through `images.get`, which cannot resolve a VID# key."""
        video = videos.create(car_id="1", video_name="a.mp4", now=self.now)
        image = images.create(car_id="1", image_name="cars/p.jpg", now=self.now)

        with self.assertRaises(NotFound):
            images.set_primary("1", video.video_id)

        ref = Car.get(keys.car_pk("1"), keys.META).primary_image_ref
        self.assertEqual(ref["id"], image.image_id)
