"""Define the Inventory Managers group in code rather than by clicking in the admin.

Run on every deploy, so this file is the source of truth: permissions added by hand in
the admin are reconciled away on the next release. That is the point - a role that can
drift silently is not a boundary you can rely on.

Members can manage stock and nothing else. They are staff but not superusers, so the
admin never renders the Authentication section for them and returns 403 on those URLs
if typed directly.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

GROUP_NAME = "Inventory Managers"

# Exactly what the role grants. Deliberately written out in full rather than derived
# from the app label, so widening it is a visible edit to this list and shows up in a
# diff and code review.
PERMISSIONS = [
    ("cars", "add_car"),
    ("cars", "change_car"),
    ("cars", "delete_car"),
    ("cars", "view_car"),
    ("cars", "add_carimage"),
    ("cars", "change_carimage"),
    ("cars", "delete_carimage"),
    ("cars", "view_carimage"),
    # Staff administration, through the StaffAccount proxy rather than auth.User, so
    # this group never holds an auth permission and the real user admin stays superuser
    # only. No delete_staffaccount: removing someone means unticking Active, which is
    # reversible and keeps their edit history readable.
    ("cars", "add_staffaccount"),
    ("cars", "change_staffaccount"),
    ("cars", "view_staffaccount"),
]

# Granting any of these would let a member edit users or hand themselves more rights,
# which would defeat the whole arrangement.
FORBIDDEN_APP_LABELS = {"auth", "admin", "contenttypes", "sessions"}


class Command(BaseCommand):
    help = f"Create or reconcile the {GROUP_NAME!r} group. Safe to re-run."

    @transaction.atomic
    def handle(self, *args, **options):
        wanted = []
        for app_label, codename in PERMISSIONS:
            try:
                wanted.append(
                    Permission.objects.get(
                        content_type__app_label=app_label, codename=codename
                    )
                )
            except Permission.DoesNotExist as exc:
                raise CommandError(
                    f"Permission {app_label}.{codename} does not exist. Run migrate "
                    "first - permissions are created by the post_migrate signal."
                ) from exc

        # A guard against this file itself being edited carelessly later.
        offending = [p for p in wanted if p.content_type.app_label in FORBIDDEN_APP_LABELS]
        if offending:
            raise CommandError(
                "Refusing to grant "
                + ", ".join(f"{p.content_type.app_label}.{p.codename}" for p in offending)
                + f": {GROUP_NAME} must not be able to manage users or permissions."
            )

        group, created = Group.objects.get_or_create(name=GROUP_NAME)

        before = set(group.permissions.values_list("id", flat=True))
        group.permissions.set(wanted)
        after = set(group.permissions.values_list("id", flat=True))

        removed = before - after
        added = after - before

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Created' if created else 'Reconciled'} {GROUP_NAME!r}: "
                f"{len(wanted)} permission(s)"
                + (f", added {len(added)}" if added else "")
                + (f", removed {len(removed)}" if removed else "")
            )
        )
        if removed:
            names = Permission.objects.filter(id__in=removed)
            for permission in names:
                self.stdout.write(
                    self.style.WARNING(
                        f"  removed {permission.content_type.app_label}."
                        f"{permission.codename} (not in this file)"
                    )
                )
