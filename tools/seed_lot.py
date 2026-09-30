"""Seed a lot to photograph: four cars covering the four states a card can be in.

    PHOTOS=<folder of jpegs> python tools/seed_lot.py

Deliberately not fixtures in the test suite. This exists to be *looked at*, so it
carries a full spec sheet, a reserved car, a car with no price and a car with one
photo, which between them are every branch the listing and detail templates have.

Point `DDB_TABLE` at a scratch table. It writes real rows.
"""
import os
import pathlib

import django
django.setup()

import itertools
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from cars.store import cars as car_store, images as image_store
from cars.store.models import Car
from cars.choices import CarStatus, FuelType
from cars.tasks import build_derivatives_task

ids = itertools.count(1)

SRC = pathlib.Path(os.environ.get("PHOTOS", "."))
photos = sorted(p for p in SRC.iterdir()
                if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
if not photos:
    raise SystemExit(f"no photographs in {SRC} -- set PHOTOS to a folder of them")

PRIUS_SPECS = [
    {"label_en": "Mileage", "label_ja": "走行距離", "value_en": "215,000+km", "value_ja": "215,000km以上"},
    {"label_en": "Shaken (inspection)", "label_ja": "車検", "value_en": "2026-11-27", "value_ja": "2026年11月27日"},
    {"label_en": "Hybrid system", "label_ja": "ハイブリッドシステム", "value_en": "Excellent", "value_ja": "良好"},
    {"label_en": "Road tax paid until", "label_ja": "自動車税納付済", "value_en": "2027-03-31", "value_ja": "2027年3月31日"},
    {"label_en": "Previous use", "label_ja": "使用状況", "value_en": "Personal running car", "value_ja": "自家用"},
    {"label_en": "Wheels", "label_ja": "ホイール", "value_en": "Alloy", "value_ja": "アルミホイール"},
    {"label_en": "Seats", "label_ja": "シート", "value_en": "Leather seat covers", "value_ja": "レザーシートカバー"},
    {"label_en": "Audio", "label_ja": "オーディオ", "value_en": "Bluetooth", "value_ja": "Bluetooth"},
    {"label_en": "Navigation", "label_ja": "ナビ", "value_en": "Navi + TV", "value_ja": "ナビ・TV付"},
    {"label_en": "Steering", "label_ja": "ステアリング", "value_en": "Power steering", "value_ja": "パワーステアリング"},
    {"label_en": "Windows", "label_ja": "窓", "value_en": "Power windows", "value_ja": "パワーウィンドウ"},
    {"label_en": "Economy", "label_ja": "燃費", "value_en": "Fuel efficient", "value_ja": "低燃費"},
]

LOT = [
    dict(brand="Toyota", model_name="Prius", brand_ja="トヨタ", model_name_ja="プリウス",
         manufacture_year=2012, price_jpy=390000, color="White", color_ja="ホワイト",
         fuel_type=FuelType.HYBRID, seat_capacity=5, grade="S", model_code="ZVW30",
         chassis_number="ZVW30-5512345", status=CarStatus.AVAILABLE, specs=PRIUS_SPECS,
         description_en="A one-owner Prius kept as a personal car. The hybrid system is "
                        "in excellent order and the inspection runs to November 2026.",
         description_ja="個人所有のワンオーナー車。ハイブリッドシステムは良好で、"
                        "車検は2026年11月まで残っています。",
         n=8),
    dict(brand="Daihatsu", model_name="Tanto", brand_ja="ダイハツ", model_name_ja="タント",
         manufacture_year=2014, price_jpy=450000, color="Pearl White", color_ja="パールホワイト",
         fuel_type=FuelType.PETROL, seat_capacity=4, grade="X", model_code="LA600S",
         chassis_number="LA600S-0112233", status=CarStatus.AVAILABLE,
         specs=[{"label_en": "Mileage", "label_ja": "走行距離",
                 "value_en": "98,000km", "value_ja": "98,000km"}],
         n=3),
    dict(brand="Honda", model_name="N-Box", brand_ja="ホンダ", model_name_ja="N-BOX",
         manufacture_year=2016, price_jpy=620000, color="Silver", color_ja="シルバー",
         fuel_type=FuelType.PETROL, seat_capacity=4, grade="G", model_code="JF1",
         chassis_number="JF1-1234567", status=CarStatus.RESERVED,
         specs=[{"label_en": "Mileage", "label_ja": "走行距離",
                 "value_en": "64,500km", "value_ja": "64,500km"}],
         n=2),
    dict(brand="Suzuki", model_name="Wagon R", brand_ja="スズキ", model_name_ja="ワゴンR",
         manufacture_year=2011, price_jpy=None, color="Blue", color_ja="ブルー",
         fuel_type=FuelType.PETROL, seat_capacity=4, grade="FX", model_code="MH23S",
         chassis_number="MH23S-7654321", status=CarStatus.AVAILABLE, n=2),
]

cursor = 0
for spec in LOT:
    n = spec.pop("n")
    car = car_store.create(Car(car_id=str(next(ids)), **spec), now=timezone.now())
    for order in range(n):
        source = photos[cursor % len(photos)]
        cursor += 1
        name = source.name.replace(" ", "-").replace("(", "").replace(")", "")
        stored = default_storage.save(f"cars/{name}", ContentFile(source.read_bytes()))
        image = image_store.create(car_id=car.car_id, image_name=stored,
                                   order=order, is_primary=(order == 0),
                                   now=timezone.now())
        build_derivatives_task(f"{car.car_id}:{image.image_id}")
    print(f"{car.slug}: {n} photos")
