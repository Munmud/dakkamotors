"""Create the production superuser without an interactive prompt.

Zappa runs management commands through Lambda, which has no TTY, so
`createsuperuser` cannot be used against the deployed environment. This command reads
credentials from the environment and is safe to run repeatedly.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or update the superuser from DJANGO_ADMIN_* environment variables."

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_ADMIN_USERNAME", "admin")
        email = os.environ.get("DJANGO_ADMIN_EMAIL", "")
        password = os.environ.get("DJANGO_ADMIN_PASSWORD")

        if not password:
            raise CommandError(
                "DJANGO_ADMIN_PASSWORD is not set; refusing to create a superuser "
                "without a password."
            )

        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=username, defaults={"email": email}
        )
        user.email = email or user.email
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Created' if created else 'Updated'} superuser {username!r}."
            )
        )
