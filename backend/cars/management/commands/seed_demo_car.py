"""Insert the sample Daihatsu Tanto listing.

Idempotent: keyed on the chassis number, so running it twice does not duplicate.
"""

from django.core.management.base import BaseCommand

from cars.models import Car, CarStatus, FuelType

DEMO_CHASSIS = "DEMO-0001"


class Command(BaseCommand):
    help = "Seed the demo Daihatsu Tanto listing (safe to re-run)."

    def handle(self, *args, **options):
        car, created = Car.objects.get_or_create(
            chassis_number=DEMO_CHASSIS,
            defaults={
                "brand": "Daihatsu",
                "model_name": "Tanto",
                "grade": "X",
                "model_code": "LA600S",
                "manufacture_year": 2018,
                "fuel_type": FuelType.PETROL,
                "seat_capacity": 4,
                "color": "Pearl White",
                "price_jpy": None,  # renders as "Call for price"
                "status": CarStatus.AVAILABLE,
                "description_en": (
                    "DEMO LISTING — placeholder data, safe to delete. "
                    "Kei-class tall wagon with sliding rear doors, well suited to city "
                    "driving and easy parking."
                ),
                "description_ja": (
                    "デモ掲載 — サンプルデータです。削除して問題ありません。"
                    "スライドドア付きの軽トールワゴン。街乗りや駐車がしやすい一台です。"
                ),
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created demo car: {car}"))
        else:
            self.stdout.write(f"Demo car already present: {car}")
