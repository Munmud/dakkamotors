"""Recompute the two maintained counters from the items that actually exist.

`Slot.booked_count` and `Customer.active_bookings` replaced two `COUNT(*)` queries taken
under a row lock. That is what makes the booking transaction atomic, but it also makes
them derived state that nothing recomputes on its own: a counter can drift if an item is
edited by hand, or restored from a backup, or written by an importer out of order.

The source of truth is unambiguous, which is what makes repair cheap:

* a slot's seats are the `LIVE#` guard items in its own partition
* a customer's active bookings are the `BOOKING#` items in theirs

**Deliberately not scheduled.** Nothing in this system runs on a timer, and a job that
reads every slot every night to find nothing wrong is exactly the kind of thing that
turns a near-zero bill into a real one. Run it after a migration, after a restore, or
when a number looks wrong.

    python manage.py reconcile_counters            # report only
    python manage.py reconcile_counters --fix
"""

import datetime as dt

from django.core.management.base import BaseCommand
from django.utils import timezone

from cars.choices import ACTIVE_STATUSES
from cars.store import keys
from cars.store.models import Booking, Customer, Seat, Slot


class Command(BaseCommand):
    help = "Check (or repair) booked_count and active_bookings."

    def add_arguments(self, parser):
        parser.add_argument("--fix", action="store_true",
                            help="Write the corrected values, not just report them.")
        parser.add_argument("--days", type=int, default=400,
                            help="How far either side of today to check slots.")

    def handle(self, *args, **options):
        fix = options["fix"]
        # Respected so the importer, which calls this at the end of every run, does not
        # narrate itself through a test suite.
        self.quiet = options.get("verbosity", 1) == 0
        drift = 0

        drift += self._slots(fix, options["days"])
        drift += self._customers(fix)

        if self.quiet:
            return
        if not drift:
            self.stdout.write(self.style.SUCCESS("Every counter agrees with its items."))
        elif fix:
            self.stdout.write(self.style.SUCCESS(f"Repaired {drift} counter(s)."))
        else:
            self.stdout.write(self.style.WARNING(
                f"{drift} counter(s) disagree. Re-run with --fix to correct them."))

    def _slots(self, fix, days):
        now = timezone.now()
        drift = 0
        for month in self._months(now - dt.timedelta(days=days),
                                  now + dt.timedelta(days=days)):
            for slot in Slot.gsi1.query(month):
                actual = sum(1 for _ in Seat.query(
                    keys.slot_pk(slot.slot_id),
                    range_key_condition=Seat.sk.startswith("LIVE#")))
                if int(slot.booked_count or 0) == actual:
                    continue
                drift += 1
                if not self.quiet:
                    self.stdout.write(
                        f"  slot {slot.slot_id}: booked_count "
                        f"{slot.booked_count} -> {actual}")
                if fix:
                    slot.update(actions=[Slot.booked_count.set(actual)])
        return drift

    def _customers(self, fix):
        drift = 0
        for customer in Customer.gsi1.query(keys.CUSTOMER_GSI1PK):
            actual = sum(
                1 for b in Booking.query(
                    customer.pk, range_key_condition=Booking.sk.startswith("BOOKING#"))
                if b.status in ACTIVE_STATUSES
            )
            if int(customer.active_bookings or 0) == actual:
                continue
            drift += 1
            if not self.quiet:
                self.stdout.write(
                    f"  customer {customer.customer_sub}: active_bookings "
                    f"{customer.active_bookings} -> {actual}")
            if fix:
                customer.update(actions=[Customer.active_bookings.set(actual)])
        return drift

    @staticmethod
    def _months(start, end):
        seen, cursor = [], start.replace(day=1)
        while cursor <= end:
            key = keys.slot_month_gsi1pk(cursor)
            if key not in seen:
                seen.append(key)
            cursor = (cursor.replace(day=28) + dt.timedelta(days=7)).replace(day=1)
        return seen
