"""Questions a buyer asks about one car, and the answer the shop writes back.

Kept out of `models.py` for the same reason `booking_models.py` is: that file holds the
inventory core, and this is a separate concern with its own domain module (`qa.py`), its
own views and its own admin. Django only autodiscovers `models.py`, so both are
re-exported from there for migrations to find.

The published pair is public content the shop owns. It is rendered on the car page and
into the JSON-LD graph, and **the person who asked is never named** - not by first name,
not by id. That is a decision about the customer's privacy, and it is enforced by the
serializer's field allowlist rather than by remembering to leave a field out.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from .models import Car


class QuestionLanguage(models.TextChoices):
    EN = "en", "English"
    JA = "ja", "日本語"


class CarQuestion(models.Model):
    car = models.ForeignKey(Car, related_name="questions", on_delete=models.CASCADE)

    # SET_NULL, not CASCADE: once a pair is published it is page content the shop owns
    # and search engines have indexed. Closing an account must not silently delete it.
    # Nullable also lets staff seed an FAQ entry nobody asked for.
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="car_questions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    question = models.TextField(
        help_text="Editable before publishing. People type phone numbers and names into "
        "free text, and this goes on a public page."
    )
    answer = models.TextField(blank=True, default="")

    answered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="answered_car_questions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
    )
    # This, not `answer != ""`, is what "has been answered" means. Fixing a typo in an
    # answer months later must not email and re-notify the customer all over again.
    answered_at = models.DateTimeField(null=True, blank=True, editable=False)

    is_published = models.BooleanField(
        default=False,
        help_text="Shows the question and your answer on the car's public page and in "
        "search results. The customer's name is never shown.",
    )

    # One stored answer, two page languages. Without this a Japanese answer renders on
    # the English page - the same problem description_en/description_ja solves for Car.
    language = models.CharField(
        max_length=5, choices=QuestionLanguage.choices, default=QuestionLanguage.EN
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The only hot query: published pairs for one car, newest first.
            models.Index(
                fields=["car", "is_published", "-created_at"], name="carq_public_idx"
            ),
        ]
        constraints = [
            # The guard that actually holds. A form's clean() does not run on the admin
            # changelist - get_changelist_form() never passes ModelAdmin.form - so a
            # tick-box bulk edit would otherwise publish a question with no answer.
            models.CheckConstraint(
                condition=Q(is_published=False) | ~Q(answer=""),
                name="published_question_has_an_answer",
            ),
        ]

    def __str__(self):
        return f"Q{self.pk} on {self.car}: {self.question[:40]}"

    @property
    def is_answered(self):
        return self.answered_at is not None

    @property
    def state(self):
        """What the shop still has to do about this one."""
        if not self.is_answered:
            return "Needs an answer"
        return "Published" if self.is_published else "Answered, not public"
