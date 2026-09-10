"""One customer's bell.

**There is deliberately no stored text field here, and adding one would be a bug.** The
site has a language switcher, so a customer who signs up in English and later switches to
Japanese must not find their old notifications frozen in English. What is stored is the
`kind` and enough `context` to render it; the words are produced on the client from the
i18n files, at read time, in whatever language is showing.

Nothing sweeps this table on a timer. Aurora scales to zero, and a scheduled cleanup would
wake it around the clock to do nothing - so `notifications.notify()` trims as it writes,
the same way `booking.ensure_slots` generates as it reads.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q


class NotificationKind(models.TextChoices):
    BOOKING_CONFIRMED = "booking_confirmed", "Test drive confirmed"
    BOOKING_CANCELLED = "booking_cancelled", "Test drive cancelled"
    QUESTION_ANSWERED = "question_answered", "Question answered"


class Notification(models.Model):
    # CASCADE, unlike CarQuestion.customer: a bell entry is worthless without its owner
    # and is nobody else's content.
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="notifications", on_delete=models.CASCADE
    )
    kind = models.CharField(max_length=32, choices=NotificationKind.choices)

    # Snapshots strings, never foreign keys - the same reason TestDriveBooking.car_label
    # exists. A car sold and deleted must not blank out somebody's history.
    context = models.JSONField(default=dict, blank=True)

    # "booking:12:confirmed". Makes notify() idempotent, which matters once an admin
    # action and a save_model can both reach the same domain function.
    dedupe_key = models.CharField(max_length=80, blank=True, default="")

    # Nullable rather than an is_read flag, so we keep *when* they saw it.
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["customer", "-created_at"], name="notif_feed_idx"),
            models.Index(
                fields=["customer"],
                condition=Q(read_at__isnull=True),
                name="notif_unread_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "dedupe_key"],
                condition=~Q(dedupe_key=""),
                name="one_notification_per_event",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} for {self.customer_id}"
