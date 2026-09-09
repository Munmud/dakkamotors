from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import Car, CarImage, CarStatus

# A 1x1 GIF — smallest thing Pillow will accept as a real image.
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def make_car(chassis, **overrides):
    fields = {
        "brand": "Daihatsu",
        "model_name": "Tanto",
        "grade": "X",
        "model_code": "LA600S",
        "chassis_number": chassis,
        "manufacture_year": 2018,
        "seat_capacity": 4,
        "color": "Pearl White",
    }
    fields.update(overrides)
    return Car.objects.create(**fields)


def attach_image(car, name, *, is_primary=False, order=0):
    return CarImage.objects.create(
        car=car,
        image=SimpleUploadedFile(name, TINY_GIF, content_type="image/gif"),
        is_primary=is_primary,
        order=order,
    )


class CarListApiTests(TestCase):
    def test_list_returns_only_available_cars(self):
        make_car("AVAIL-1", status=CarStatus.AVAILABLE)
        make_car("RESERVED-1", status=CarStatus.RESERVED)
        make_car("SOLD-1", status=CarStatus.SOLD)

        response = self.client.get(reverse("car-list"))

        self.assertEqual(response.status_code, 200)
        chassis = {c["id"] for c in response.json()["results"]}
        self.assertEqual(len(chassis), 1)
        self.assertEqual(response.json()["count"], 1)

    def test_blank_price_serialises_as_null_not_zero(self):
        make_car("NO-PRICE", price_jpy=None)

        result = self.client.get(reverse("car-list")).json()["results"][0]

        self.assertIsNone(result["price_jpy"])

    def test_list_includes_primary_image(self):
        car = make_car("WITH-IMAGE")
        attach_image(car, "b.gif", order=1)
        primary = attach_image(car, "a.gif", is_primary=True, order=2)

        result = self.client.get(reverse("car-list")).json()["results"][0]

        self.assertIsNotNone(result["primary_image"])
        self.assertEqual(result["primary_image"]["id"], primary.id)

    def test_list_primary_image_is_null_when_car_has_no_images(self):
        make_car("NO-IMAGE")

        result = self.client.get(reverse("car-list")).json()["results"][0]

        self.assertIsNone(result["primary_image"])


class CarDetailApiTests(TestCase):
    def test_detail_is_reachable_for_a_reserved_car(self):
        """A shared link must keep working after the car is reserved or sold."""
        car = make_car("RESERVED-2", status=CarStatus.RESERVED)

        response = self.client.get(reverse("car-detail", args=[car.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "reserved")

    def test_detail_returns_full_gallery_in_order(self):
        car = make_car("GALLERY")
        second = attach_image(car, "second.gif", order=2)
        first = attach_image(car, "first.gif", order=1)

        images = self.client.get(reverse("car-detail", args=[car.pk])).json()["images"]

        self.assertEqual([i["id"] for i in images], [first.id, second.id])

    def test_detail_exposes_both_descriptions(self):
        car = make_car("DESCRIPTIONS", description_en="English", description_ja="日本語")

        payload = self.client.get(reverse("car-detail", args=[car.pk])).json()

        self.assertEqual(payload["description_en"], "English")
        self.assertEqual(payload["description_ja"], "日本語")


class PrimaryImageFallbackTests(TestCase):
    def test_falls_back_to_lowest_order_when_nothing_is_flagged(self):
        car = make_car("FALLBACK")
        attach_image(car, "third.gif", order=3)
        lowest = attach_image(car, "first.gif", order=1)

        self.assertEqual(car.primary_image, lowest)

    def test_returns_none_when_there_are_no_images(self):
        self.assertIsNone(make_car("EMPTY").primary_image)
