"""Dump everything in Postgres to NDJSON, ready for the DynamoDB importer.

Deliberately two artifacts rather than one command. The exporter needs the ORM and the
ORM is on its way out, so this runs from a commit that still has it while
`import_dynamo` runs on the new code. Trying to do both in one process would mean
keeping the models alive purely to migrate off them.

Writes one file per model plus a manifest carrying counts and a SHA-256 of each file, so
the import can prove it read what the export wrote rather than assuming it.

    python manage.py export_aurora --out ./migration/2026-09-17/
"""

import datetime as dt
import hashlib
import json
import pathlib

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model


def _iso(value):
    return value.isoformat() if isinstance(value, (dt.datetime, dt.date, dt.time)) else value


class Command(BaseCommand):
    help = "Export every Postgres row to NDJSON for the DynamoDB migration."

    def add_arguments(self, parser):
        parser.add_argument("--out", required=True,
                            help="Directory to write the NDJSON files into.")

    def handle(self, *args, **options):
        out = pathlib.Path(options["out"])
        out.mkdir(parents=True, exist_ok=True)

        try:
            from cars.models import Car, CarImage
            from cars.booking_models import (
                CustomerProfile, TestDriveBooking, TestDriveSchedule, TestDriveSlot,
            )
            from cars.notification_models import Notification
            from cars.qa_models import CarQuestion
        except Exception as exc:  # pragma: no cover - only on the new code
            raise CommandError(
                "The ORM models are gone from this checkout. Run this from the commit "
                "tagged before the DynamoDB cutover."
            ) from exc

        manifest = {}
        manifest["users"] = self._dump(out, "users", (
            {
                "pk": u.pk, "username": u.username, "email": u.email,
                "first_name": u.first_name, "last_name": u.last_name,
                "is_active": u.is_active, "is_staff": u.is_staff,
                "is_superuser": u.is_superuser,
                # Carried so a Cognito UserMigration trigger can verify the existing
                # password on first sign-in rather than forcing everyone to reset.
                "password": u.password,
                "date_joined": _iso(u.date_joined),
            }
            for u in get_user_model().objects.all().order_by("pk")
        ))

        manifest["profiles"] = self._dump(out, "profiles", (
            {"user_id": p.user_id, "phone": p.phone, "created_at": _iso(p.created_at)}
            for p in CustomerProfile.objects.all().order_by("pk")
        ))

        manifest["cars"] = self._dump(out, "cars", (
            {
                "pk": c.pk, "brand": c.brand, "grade": c.grade,
                "model_name": c.model_name, "model_code": c.model_code,
                "chassis_number": c.chassis_number,
                "manufacture_year": c.manufacture_year, "fuel_type": c.fuel_type,
                "seat_capacity": c.seat_capacity, "color": c.color,
                "price_jpy": c.price_jpy, "status": c.status,
                "description_en": c.description_en, "description_ja": c.description_ja,
                "video_name": c.video.name if c.video else "",
                "video_uploaded_at": _iso(c.video_uploaded_at),
                "slug": c.slug,
                "created_at": _iso(c.created_at), "updated_at": _iso(c.updated_at),
            }
            for c in Car.objects.all().order_by("pk")
        ))

        manifest["images"] = self._dump(out, "images", (
            {
                "pk": i.pk, "car_id": i.car_id,
                "image_name": i.image.name if i.image else "",
                "is_primary": i.is_primary, "order": i.order,
                "derivatives_ready": i.derivatives_ready,
                "derivative_widths": i.available_widths,
            }
            for i in CarImage.objects.all().order_by("pk")
        ))

        manifest["schedules"] = self._dump(out, "schedules", (
            {
                "pk": s.pk, "weekday": s.weekday,
                "start_time": _iso(s.start_time), "end_time": _iso(s.end_time),
                "capacity": s.capacity, "is_active": s.is_active,
                "starts_on": _iso(s.starts_on), "ends_on": _iso(s.ends_on),
                "note": s.note,
            }
            for s in TestDriveSchedule.objects.all().order_by("pk")
        ))

        manifest["slots"] = self._dump(out, "slots", (
            {
                "pk": s.pk, "schedule_id": s.schedule_id,
                "starts_at": _iso(s.starts_at), "ends_at": _iso(s.ends_at),
                "capacity": s.capacity, "is_open": s.is_open,
            }
            for s in TestDriveSlot.objects.all().order_by("pk")
        ))

        manifest["bookings"] = self._dump(out, "bookings", (
            {
                "pk": b.pk, "slot_id": b.slot_id, "customer_id": b.customer_id,
                "car_id": b.car_id, "car_label": b.car_label, "status": b.status,
                "confirmed_at": _iso(b.confirmed_at),
                "cancelled_at": _iso(b.cancelled_at),
                "created_at": _iso(b.created_at), "updated_at": _iso(b.updated_at),
            }
            for b in TestDriveBooking.objects.all().order_by("pk")
        ))

        manifest["questions"] = self._dump(out, "questions", (
            {
                "pk": q.pk, "car_id": q.car_id, "customer_id": q.customer_id,
                "question": q.question, "answer": q.answer,
                "answered_by_id": q.answered_by_id,
                "answered_at": _iso(q.answered_at),
                "is_published": q.is_published, "language": q.language,
                "created_at": _iso(q.created_at), "updated_at": _iso(q.updated_at),
            }
            for q in CarQuestion.objects.all().order_by("pk")
        ))

        manifest["notifications"] = self._dump(out, "notifications", (
            {
                "pk": n.pk, "customer_id": n.customer_id, "kind": n.kind,
                "context": n.context, "dedupe_key": n.dedupe_key,
                "read_at": _iso(n.read_at), "created_at": _iso(n.created_at),
            }
            for n in Notification.objects.all().order_by("pk")
        ))

        (out / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")

        total = sum(entry["count"] for entry in manifest.values())
        self.stdout.write(self.style.SUCCESS(
            f"Exported {total} rows across {len(manifest)} files to {out}."))

    def _dump(self, out, name, rows):
        path = out / f"{name}.ndjson"
        count = 0
        digest = hashlib.sha256()
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                line = json.dumps(row, ensure_ascii=False, default=str)
                fh.write(line + "\n")
                digest.update(line.encode("utf-8"))
                count += 1
        self.stdout.write(f"  {name}: {count}")
        return {"count": count, "sha256": digest.hexdigest()}
