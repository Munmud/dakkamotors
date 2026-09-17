"""Load an `export_aurora` dump into DynamoDB.

Idempotent: entity items are plain puts, so a re-run overwrites rather than duplicates,
and guard items tolerate already existing. That matters because the useful way to
rehearse a migration is to run it repeatedly against a snapshot restore until it is
boring.

Order is not arbitrary. Cars must exist before their photos and questions, slots before
bookings, customers before the counters that bookings increment.

    python manage.py import_dynamo --from ./migration/2026-09-17/

Run it from a laptop with SSO credentials, not through `zappa manage`: DynamoDB is not
in a VPC, so nothing about this needs a Lambda.
"""

import datetime as dt
import json
import pathlib

from django.core.management.base import BaseCommand, CommandError

from cars.choices import ACTIVE_STATUSES
from cars.store import auth as auth_store
from cars.store import cars as car_store
from cars.store import customers as customer_store
from cars.store import images as image_store
from cars.store import keys
from cars.store import schedules as schedule_store
from cars.store import slots as slot_store
from cars.store.models import (
    Booking, Car, CarImage, CarQuestion, ChassisGuard, Customer, LegacyCarPointer,
    Notification, Seat, SlugGuard,
)


def _dt(value):
    return dt.datetime.fromisoformat(value) if value else None


def _date(value):
    return dt.date.fromisoformat(value) if value else None


def _time(value):
    return dt.time.fromisoformat(value) if value else None


class Command(BaseCommand):
    help = "Import an export_aurora dump into DynamoDB."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="source", required=True)
        parser.add_argument(
            "--subs", dest="subs",
            help="JSON mapping of Django user id -> Cognito sub. Without it, the user "
                 "id is used as the sub, which is what the identity bridge assumes "
                 "while Django auth is still in place.",
        )

    def handle(self, *args, **options):
        source = pathlib.Path(options["source"])
        if not (source / "manifest.json").exists():
            raise CommandError(f"No manifest.json in {source}.")
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))

        subs = {}
        if options["subs"]:
            subs = {str(k): str(v) for k, v in
                    json.loads(pathlib.Path(options["subs"]).read_text("utf-8")).items()}

        def sub_for(user_id):
            return subs.get(str(user_id), str(user_id)) if user_id is not None else None

        rows = {name: self._read(source, name, manifest)
                for name in manifest}

        users = {str(u["pk"]): u for u in rows.get("users", [])}
        phones = {str(p["user_id"]): p for p in rows.get("profiles", [])}

        counts = {}
        counts["customers"] = self._customers(rows, users, phones, sub_for)
        car_ids = self._cars(rows.get("cars", []))
        counts["cars"] = len(car_ids)
        counts["images"] = self._images(rows.get("images", []))
        schedule_ids = self._schedules(rows.get("schedules", []))
        counts["schedules"] = len(schedule_ids)
        slot_ids = self._slots(rows.get("slots", []), schedule_ids)
        counts["slots"] = len(slot_ids)
        counts["bookings"] = self._bookings(
            rows.get("bookings", []), slot_ids, users, phones, sub_for)
        counts["questions"] = self._questions(rows.get("questions", []), users, sub_for)
        counts["notifications"] = self._notifications(
            rows.get("notifications", []), sub_for)

        self._reconcile()

        for name, n in counts.items():
            self.stdout.write(f"  {name}: {n}")
        self.stdout.write(self.style.SUCCESS("Import complete; counters reconciled."))

    # -- reading -------------------------------------------------------------------

    def _read(self, source, name, manifest):
        path = source / f"{name}.ndjson"
        if not path.exists():
            return []
        lines = [l for l in path.read_text(encoding="utf-8").split("\n") if l.strip()]
        expected = manifest[name]["count"]
        if len(lines) != expected:
            raise CommandError(
                f"{name}.ndjson has {len(lines)} rows, manifest says {expected}.")
        return [json.loads(l) for l in lines]

    # -- entities ------------------------------------------------------------------

    def _customers(self, rows, users, phones, sub_for):
        n = 0
        for user in rows.get("users", []):
            if user["is_staff"] or user["is_superuser"]:
                # Staff are people, not customers: they hold no bookings and do not
                # belong in the customer list.
                continue
            sub = sub_for(user["pk"])
            email = (user["email"] or "").lower()
            Customer(
                pk=keys.customer_pk(sub), sk=keys.PROFILE, customer_sub=sub,
                email=email, active_bookings=0,
                created_at=_dt(user["date_joined"]),
                gsi1pk=keys.CUSTOMER_GSI1PK, gsi1sk=email,
            ).save()

            # The carried-over password, read once by the Cognito UserMigration trigger
            # on this customer's first sign-in and deleted there. Without it every
            # existing customer would get an unprompted "your password no longer works"
            # email on the day of the cutover.
            if email and user.get("password") and user["is_active"]:
                profile = phones.get(str(user["pk"]), {})
                name = " ".join(filter(None, [user.get("first_name"),
                                              user.get("last_name")])).strip()
                auth_store.remember_legacy_password(
                    email=email, password_hash=user["password"],
                    name=name, phone=profile.get("phone", ""),
                    now=dt.datetime.now(dt.timezone.utc),
                )
            n += 1
        return n

    def _cars(self, rows):
        ids = {}
        for row in rows:
            car_id = str(row["pk"])
            ids[car_id] = car_id
            created, updated = _dt(row["created_at"]), _dt(row["updated_at"])
            car = Car(
                pk=keys.car_pk(car_id), sk=keys.META, car_id=car_id,
                brand=row["brand"], grade=row["grade"], model_name=row["model_name"],
                model_code=row["model_code"], chassis_number=row["chassis_number"],
                manufacture_year=row["manufacture_year"], fuel_type=row["fuel_type"],
                seat_capacity=row["seat_capacity"], color=row["color"],
                price_jpy=row["price_jpy"], status=row["status"],
                description_en=row["description_en"],
                description_ja=row["description_ja"],
                video_name=row["video_name"] or None,
                video_uploaded_at=_dt(row["video_uploaded_at"]),
                slug=row["slug"], created_at=created, updated_at=updated,
                gsi1pk=keys.car_status_gsi1pk(row["status"]),
                gsi1sk=keys.car_gsi1sk(created, car_id),
            )
            car.search_blob = car.build_search_blob()
            car.save()
            SlugGuard(pk=keys.slug_pk(row["slug"]), sk="SLUG", car_id=car_id).save()
            if row["chassis_number"]:
                ChassisGuard(pk=keys.chassis_guard_pk(row["chassis_number"]),
                             sk=keys.GUARD, car_id=car_id).save()
            # So a link shared as /cars/34 still resolves after ids stop being numeric.
            LegacyCarPointer(pk=keys.legacy_car_pk(row["pk"]), sk=keys.META,
                             slug=row["slug"], car_id=car_id).save()
        return ids

    def _images(self, rows):
        touched = set()
        for row in rows:
            car_id, image_id = str(row["car_id"]), str(row["pk"])
            image = CarImage(
                pk=keys.car_pk(car_id), sk=keys.image_sk(image_id),
                image_id=image_id, car_id=car_id, image_name=row["image_name"],
                is_primary=row["is_primary"], order=row["order"],
                derivatives_ready=row["derivatives_ready"],
                derivative_widths=[int(w) for w in row["derivative_widths"]] or None,
            )
            if not row["derivatives_ready"] and row["image_name"]:
                # Back into the sparse pending index, so the repair command finds it.
                image.gsi1pk = keys.IMAGE_PENDING_GSI1PK
                image.gsi1sk = image_id
            image.save()
            touched.add(car_id)
        for car_id in touched:
            image_store.refresh_primary(car_id)
        return len(rows)

    def _schedules(self, rows):
        ids = {}
        for row in rows:
            schedule_id = str(row["pk"])
            ids[str(row["pk"])] = schedule_id
            start, end = _time(row["start_time"]), _time(row["end_time"])
            from cars.store.models import RuleGuard, Schedule
            Schedule(
                pk=keys.schedule_pk(schedule_id), sk=keys.META,
                schedule_id=schedule_id, weekday=row["weekday"],
                start_time=start, end_time=end, capacity=row["capacity"],
                is_active=row["is_active"], starts_on=_date(row["starts_on"]),
                ends_on=_date(row["ends_on"]), note=row["note"],
                gsi1pk=keys.SCHEDULE_GSI1PK,
                gsi1sk=keys.schedule_gsi1sk(row["weekday"], start, schedule_id),
            ).save()
            RuleGuard(pk=keys.rule_guard_pk(row["weekday"], start, end),
                      sk=keys.GUARD, schedule_id=schedule_id).save()
        return ids

    def _slots(self, rows, schedule_ids):
        """Slot ids change: they become (schedule, start time) rather than a sequence.

        That is what makes generating them idempotent, and it is the one id in the whole
        migration that is not preserved. The mapping is built here because bookings
        reference the old one.
        """
        ids = {}
        for row in rows:
            starts = _dt(row["starts_at"])
            schedule_id = schedule_ids.get(str(row["schedule_id"]))
            # A slot whose rule was deleted keeps working; it just has no rule.
            sid = keys.slot_id(schedule_id or f"orphan-{row['pk']}", starts)
            ids[str(row["pk"])] = sid
            slot_store.Slot(
                pk=keys.slot_pk(sid), sk=keys.META, slot_id=sid,
                schedule_id=schedule_id, starts_at=starts,
                ends_at=_dt(row["ends_at"]), capacity=row["capacity"],
                is_open=row["is_open"], booked_count=0,
                gsi1pk=keys.slot_month_gsi1pk(starts),
                gsi1sk=keys.slot_gsi1sk(starts, sid),
            ).save()
        return ids

    def _bookings(self, rows, slot_ids, users, phones, sub_for):
        for row in rows:
            sub = sub_for(row["customer_id"])
            sid = slot_ids.get(str(row["slot_id"]))
            if sid is None:
                self.stderr.write(
                    f"booking {row['pk']}: slot {row['slot_id']} missing, skipped")
                continue
            slot = slot_store.find(sid)
            user = users.get(str(row["customer_id"]), {})
            name = " ".join(filter(None, [user.get("first_name"),
                                          user.get("last_name")])).strip()
            snap = {
                "customer_name": name or user.get("username", ""),
                "customer_email": user.get("email", ""),
                "customer_phone": phones.get(str(row["customer_id"]), {}).get("phone", ""),
            }
            booking_id = str(row["pk"])
            Booking(
                pk=keys.customer_pk(sub), sk=keys.booking_sk(booking_id),
                booking_id=booking_id, customer_sub=sub, slot_id=sid,
                slot_starts_at=slot.starts_at if slot else None,
                slot_ends_at=slot.ends_at if slot else None,
                car_id=str(row["car_id"]) if row["car_id"] else None,
                car_label=row["car_label"], status=row["status"],
                confirmed_at=_dt(row["confirmed_at"]),
                cancelled_at=_dt(row["cancelled_at"]),
                created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
                gsi1pk=keys.booking_status_gsi1pk(row["status"]),
                gsi1sk=keys.booking_gsi1sk(
                    slot.starts_at if slot else _dt(row["created_at"]), sid, booking_id),
                **snap,
            ).save()

            # The live-booking guard exists only while the booking holds a seat.
            if row["status"] in ACTIVE_STATUSES:
                Seat(pk=keys.slot_pk(sid), sk=keys.seat_sk(sub),
                     booking_id=booking_id, customer_sub=sub,
                     car_label=row["car_label"], created_at=_dt(row["created_at"]),
                     **snap).save()
        return len(rows)

    def _questions(self, rows, users, sub_for):
        for row in rows:
            car_id = str(row["car_id"])
            car = car_store.find(car_id)
            created = _dt(row["created_at"])
            question_id = str(row["pk"])
            sub = sub_for(row["customer_id"]) if row["customer_id"] else None
            question = CarQuestion(
                pk=keys.car_pk(car_id), sk=keys.question_sk(created, question_id),
                question_id=question_id, car_id=car_id,
                car_brand=car.brand if car else "",
                car_model_name=car.model_name if car else "",
                car_slug=car.slug if car else "",
                car_label=str(car) if car else "",
                customer_sub=sub,
                customer_email=users.get(str(row["customer_id"]), {}).get("email", ""),
                question=row["question"], answer=row["answer"],
                answered_by=str(row["answered_by_id"]) if row["answered_by_id"] else None,
                answered_at=_dt(row["answered_at"]),
                is_published=row["is_published"], language=row["language"],
                created_at=created, updated_at=_dt(row["updated_at"]),
                gsi1pk=keys.QUESTION_GSI1PK,
                gsi1sk=keys.question_gsi1sk(created, car_id, question_id),
                gsi2pk=keys.customer_pk(sub) if sub else None,
                gsi2sk=(f"Q#{keys.iso(created)}#{car_id}#{question_id}"
                        if sub else None),
            )
            question.search_blob = question.build_search_blob()
            question.save()
        return len(rows)

    def _notifications(self, rows, sub_for):
        from cars.store.models import DedupeGuard
        from cars.store.notifications import KEEP_DAYS

        for row in rows:
            sub = sub_for(row["customer_id"])
            created = _dt(row["created_at"])
            notification_id = str(row["pk"])
            sk = keys.notification_sk(created, notification_id)
            ttl = int((created + dt.timedelta(days=KEEP_DAYS)).timestamp())
            Notification(
                pk=keys.customer_pk(sub), sk=sk, notification_id=notification_id,
                customer_sub=sub, kind=row["kind"], context=row["context"],
                dedupe_key=row["dedupe_key"], read_at=_dt(row["read_at"]),
                created_at=created, ttl=ttl,
            ).save()
            if row["dedupe_key"]:
                DedupeGuard(pk=keys.customer_pk(sub),
                            sk=keys.dedupe_sk(row["dedupe_key"]),
                            notification_sk=sk, ttl=ttl).save()
        return len(rows)

    # -- counters ------------------------------------------------------------------

    def _reconcile(self):
        """Recompute the maintained counters from what actually landed.

        `booked_count` and `active_bookings` are derived state that nothing recomputes
        on its own, and the import writes the items that feed them out of order. Same
        code as the reconcile_counters command, which exists for the same reason.
        """
        from django.core.management import call_command

        # --fix, not a dry run. The importer writes bookings and seats in an order that
        # leaves both counters at zero, so reporting the drift and leaving it would
        # hand over a table where every slot looks empty and nobody can book.
        call_command("reconcile_counters", fix=True, verbosity=0)
