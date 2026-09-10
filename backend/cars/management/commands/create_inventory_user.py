"""Create a staff account that can manage stock and nothing else.

Zappa runs management commands through Lambda, which has no TTY, so `createsuperuser`
and friends are unusable against the deployed environment. The initial password arrives
through the environment for the same reason `create_admin_user` does: the function sits
in a private subnet with only an S3 gateway endpoint and cannot reach the SSM API at
all, so `remote_env` (a private encrypted object in S3) is the only channel available.

Written to be reused for the next hire rather than hardcoded to one person.
"""

import os

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from .ensure_inventory_group import GROUP_NAME


class Command(BaseCommand):
    help = f"Create a staff user in the {GROUP_NAME!r} group. Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--email", default="")
        parser.add_argument("--first-name", default="")
        parser.add_argument("--last-name", default="")

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"]

        try:
            group = Group.objects.get(name=GROUP_NAME)
        except Group.DoesNotExist as exc:
            raise CommandError(
                f"The {GROUP_NAME!r} group does not exist yet. "
                "Run `manage.py ensure_inventory_group` first."
            ) from exc

        user = User.objects.filter(username=username).first()

        # A mistyped username matching the owner's account must fail loudly rather than
        # quietly stripping its superuser flag and locking everyone out of user admin.
        if user is not None and user.is_superuser:
            raise CommandError(
                f"{username!r} is a superuser. Refusing to modify it - pick a different "
                "username, or change the account by hand if this is really intended."
            )

        created = user is None
        if created:
            password = os.environ.get("INVENTORY_USER_PASSWORD")
            if not password:
                raise CommandError(
                    "INVENTORY_USER_PASSWORD is not set; refusing to create an account "
                    "without a password."
                )
            user = User(username=username)
            user.set_password(password)

        user.email = options["email"] or user.email
        user.first_name = options["first_name"] or user.first_name
        user.last_name = options["last_name"] or user.last_name
        user.is_staff = True       # required to reach the admin at all
        user.is_superuser = False  # the whole point of the role
        user.is_active = True
        user.save()

        user.groups.add(group)

        if created:
            self.stdout.write(
                self.style.SUCCESS(f"Created staff user {username!r} in {GROUP_NAME!r}.")
            )
        else:
            # Re-running must never reset a password the person has since changed.
            self.stdout.write(
                self.style.SUCCESS(
                    f"Updated staff user {username!r} in {GROUP_NAME!r}. "
                    "Existing password left untouched."
                )
            )
