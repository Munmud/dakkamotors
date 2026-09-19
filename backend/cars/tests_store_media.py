"""One gallery out of two item types.

`videos.py` proves a video cannot reach the listing card. This file proves the opposite
direction: that ordering the two types *together* still keeps the card correct, which is
the part that is easy to get wrong because the reorder never touches the card photo
itself -- it moves a video, and the photo underneath it changes rank as a side effect.
"""

import datetime as dt

from .store import cars, images, media, videos
from .store.models import Car
from .store import keys
from .tests_store import DynamoTestCase, count_dynamo_calls

UTC = dt.timezone.utc


def make_car(car_id):
    return Car(
        car_id=car_id,
        brand="Daihatsu",
        model_name="Tanto",
        manufacture_year=2008,
        chassis_number=f"M-{car_id}",
        status="available",
    )


class GalleryTests(DynamoTestCase):
    def setUp(self):
        super().setUp()
        cars.clear_slug_cache()
        self.now = dt.datetime.now(UTC)
        self.car = cars.create(make_car("1"), now=self.now)

    def names(self, entries):
        return [
            (kind, item.image_name if kind == media.PHOTO else item.video_name)
            for kind, item in entries
        ]

    def test_gallery_interleaves_both_types_by_order(self):
        images.create(car_id="1", image_name="a.jpg", order=0, now=self.now)
        videos.create(car_id="1", video_name="b.mp4", order=1, now=self.now)
        images.create(car_id="1", image_name="c.jpg", order=2, now=self.now)

        self.assertEqual(
            self.names(media.gallery("1")),
            [("photo", "a.jpg"), ("video", "b.mp4"), ("photo", "c.jpg")],
        )

    def test_a_video_can_lead(self):
        images.create(car_id="1", image_name="a.jpg", order=1, now=self.now)
        videos.create(car_id="1", video_name="b.mp4", order=0, now=self.now)

        self.assertEqual(media.gallery("1")[0][0], media.VIDEO)

    def test_an_empty_car_has_an_empty_gallery(self):
        self.assertEqual(media.gallery("1"), [])


class SetOrderTests(DynamoTestCase):
    def setUp(self):
        super().setUp()
        cars.clear_slug_cache()
        self.now = dt.datetime.now(UTC)
        self.car = cars.create(make_car("1"), now=self.now)
        self.a = images.create(car_id="1", image_name="a.jpg", order=0, now=self.now)
        self.b = images.create(car_id="1", image_name="b.jpg", order=1, now=self.now)
        self.clip = videos.create(car_id="1", video_name="v.mp4", order=2, now=self.now)

    def ref(self):
        return Car.get(keys.car_pk("1"), keys.META).primary_image_ref

    def test_positions_are_assigned_across_both_types(self):
        """One number space, so no photo and video share a position."""
        media.set_order("1", [
            (media.VIDEO, self.clip.video_id),
            (media.PHOTO, self.b.image_id),
            (media.PHOTO, self.a.image_id),
        ])

        self.assertEqual(videos.get("1", self.clip.video_id).order, 0)
        self.assertEqual(images.get("1", self.b.image_id).order, 1)
        self.assertEqual(images.get("1", self.a.image_id).order, 2)

    def test_leading_with_a_video_moves_the_card_to_the_next_photo(self):
        """The seventh path into the invariant, and the reason `media.py` exists.

        Nothing here touches a photo's primary flag. The card changes because putting the
        video first re-ranks the photos beneath it, and `refresh_primary` is what notices.
        """
        self.assertEqual(self.ref()["name"], "a.jpg")

        media.set_order("1", [
            (media.VIDEO, self.clip.video_id),
            (media.PHOTO, self.b.image_id),
            (media.PHOTO, self.a.image_id),
        ])

        self.assertEqual(self.ref()["name"], "b.jpg")

    def test_the_card_never_becomes_the_video(self):
        """Even ordered first, and even though `set_order` has just written its row."""
        media.set_order("1", [
            (media.VIDEO, self.clip.video_id),
            (media.PHOTO, self.a.image_id),
            (media.PHOTO, self.b.image_id),
        ])

        ref = self.ref()
        self.assertEqual(ref["name"], "a.jpg")
        self.assertTrue(ref["sk"].startswith("IMG#"))


class DetailTests(DynamoTestCase):
    """`cars.detail` is where the gallery is actually read on the car page."""

    def setUp(self):
        super().setUp()
        cars.clear_slug_cache()
        self.now = dt.datetime.now(UTC)
        self.car = cars.create(make_car("1"), now=self.now)

    def test_detail_carries_the_merged_gallery(self):
        images.create(car_id="1", image_name="a.jpg", order=0, now=self.now)
        videos.create(car_id="1", video_name="b.mp4", order=1, now=self.now)

        car = cars.detail("1")

        self.assertEqual([kind for kind, _ in car.media], ["photo", "video"])

    def test_images_stays_photos_only(self):
        """`Car.primary_image` walks `car.images` and would happily return a video."""
        images.create(car_id="1", image_name="a.jpg", order=1, now=self.now)
        videos.create(car_id="1", video_name="b.mp4", order=0, now=self.now)

        car = cars.detail("1")

        self.assertEqual([i.image_name for i in car.images], ["a.jpg"])
        self.assertEqual([v.video_name for v in car.videos], ["b.mp4"])
        self.assertEqual(car.primary_image.image_name, "a.jpg")

    def test_videos_cost_no_extra_round_trip(self):
        """Ten videos must cost what none do: they are in the partition already."""
        images.create(car_id="1", image_name="a.jpg", now=self.now)

        with count_dynamo_calls() as before:
            cars.detail("1")

        for i in range(10):
            videos.create(car_id="1", video_name=f"{i}.mp4", order=i, now=self.now)

        with count_dynamo_calls() as after:
            car = cars.detail("1")

        self.assertEqual(len(after), len(before))
        self.assertEqual(len(car.media), 11)
