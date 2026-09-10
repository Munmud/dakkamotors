"""The rules for asking and answering questions about a car.

Thin views, same as `booking.py`: every check lives here, so it applies whether the
request arrives from the app, from curl, from the admin, or from a test.

Two rules carry most of the weight:

**A question cannot be published without an answer.** Enforced three times over, because
the admin changelist is a real bypass - `ModelAdmin.get_changelist_form()` never passes
`ModelAdmin.form`, so a form's `clean()` does not run there. `publish()` is the check
staff meet, and the database CheckConstraint is the one that actually holds.

**A customer is emailed once, when their question is first answered.** Not every time the
answer is saved. Staff fix typos, and nobody wants three emails about one sentence.
"""

import datetime as dt

from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from . import mail
from . import notifications
from .models import Car
from .notification_models import NotificationKind
from .qa_models import CarQuestion

# Roughly a paragraph. Long enough for a real question about a car's history, short
# enough that the box does not invite an essay onto a public page.
MAX_QUESTION_LENGTH = 1000

# Unanswered, across all cars. The throttle limits the rate; this limits the backlog one
# person can build, which is what actually costs staff time.
MAX_OPEN_QUESTIONS = 5


class QuestionError(Exception):
    """Something a customer or a staff member can read and act on."""


def ask(*, user, car, question, language="en"):
    """Record a question and tell the shop about it.

    Returns the CarQuestion. The staff email is queued after commit, so a failure to
    write the row cannot send a message about a question that does not exist.
    """
    text = (question or "").strip()
    if not text:
        raise QuestionError("Please type your question first.")
    if len(text) > MAX_QUESTION_LENGTH:
        raise QuestionError(
            f"Please keep your question under {MAX_QUESTION_LENGTH} characters. "
            "If there is a lot to go through, call us and we will talk it over."
        )

    open_questions = CarQuestion.objects.filter(
        customer=user, answered_at__isnull=True
    ).count()
    if open_questions >= MAX_OPEN_QUESTIONS:
        raise QuestionError(
            f"You have {MAX_OPEN_QUESTIONS} questions with us already. "
            "We will answer those first."
        )

    with transaction.atomic():
        record = CarQuestion.objects.create(
            car=car,
            customer=user,
            question=text,
            language="ja" if language == "ja" else "en",
        )
        transaction.on_commit(lambda: mail.notify_staff_of_question(record))

    return record


def record_answer(question, *, answer, staff=None, now=None):
    """Save an answer, and if it is the first one, tell the customer.

    `answered_at` is what "has been answered" means, not a non-empty answer field. That
    is the difference between fixing a typo and sending someone their third email about
    the same sentence. The row is locked so a double-submitted admin form, or an action
    and a save racing each other, still produces exactly one email.
    """
    now = now or timezone.now()
    text = (answer or "").strip()

    with transaction.atomic():
        current = CarQuestion.objects.select_for_update().get(pk=question.pk)
        first_answer = current.answered_at is None and bool(text)

        current.answer = text
        fields = ["answer", "updated_at"]
        if first_answer:
            current.answered_at = now
            current.answered_by = staff
            fields += ["answered_at", "answered_by"]
        current.save(update_fields=fields)

        if first_answer and current.customer_id:
            # Inside the transaction, so it rolls back with the answer. The email is
            # deferred to on_commit so a rollback cannot send a phantom message.
            notifications.notify(
                user=current.customer,
                kind=NotificationKind.QUESTION_ANSWERED,
                context={
                    "car_label": str(current.car),
                    "car_slug": current.car.slug,
                    "question_id": current.pk,
                },
                dedupe_key=f"question:{current.pk}:answered",
                now=now,
            )
            transaction.on_commit(lambda: mail.notify_customer_of_answer(current))

    question.refresh_from_db()
    return question


def publish(question, *, now=None):
    """Put a question and its answer on the car's public page.

    Also stamps the car as updated: sitemap.xml takes its lastmod from Car.updated_at,
    and new content on a page that still reports last year's date is new content a
    crawler has no reason to come back for.
    """
    now = now or timezone.now()
    if not (question.answer or "").strip():
        raise QuestionError(
            "Write an answer before publishing. A published question with no answer is "
            "worse than no question at all."
        )

    with transaction.atomic():
        question.is_published = True
        question.save(update_fields=["is_published", "updated_at"])
        # .update() rather than car.save(): auto_now would be bypassed either way, so
        # the value is set explicitly and no other field is touched.
        Car.objects.filter(pk=question.car_id).update(updated_at=now)

    return question


def unpublish(question, *, now=None):
    now = now or timezone.now()
    question.is_published = False
    question.save(update_fields=["is_published", "updated_at"])
    Car.objects.filter(pk=question.car_id).update(updated_at=now)
    return question


def published_questions_prefetch():
    """Attach published pairs as `published_questions`, newest first.

    A named `to_attr` rather than a plain prefetch, so `published_for` can read the list
    straight off the object. Filtering the related manager at render time would silently
    ignore the prefetch and go back to the database once per car.
    """
    return Prefetch(
        "questions",
        queryset=CarQuestion.objects.filter(is_published=True).order_by("-created_at"),
        to_attr="published_questions",
    )


def published_for(car):
    """The pairs a visitor may see, newest first.

    Reads the `published_questions` attribute when a caller has prefetched it, so a page
    render stays at one query however many pairs a car has. Falling back to a filter
    here would quietly defeat that prefetch.
    """
    prefetched = getattr(car, "published_questions", None)
    if prefetched is not None:
        return prefetched
    return list(car.questions.filter(is_published=True).order_by("-created_at"))


def visible_in(questions, language):
    """Pairs written in the language the page is being served in.

    A question asked in Japanese and answered in Japanese is noise on the English page.
    But an empty section is worse than a wrong-language one, so if filtering leaves
    nothing, everything published is shown instead.
    """
    matching = [q for q in questions if q.language == language]
    return matching or list(questions)


def sweep_unanswered(older_than_days=180, now=None):
    """Housekeeping for the management command, never on a timer.

    Aurora scales to zero; a scheduled sweep would wake it daily to do nothing.
    """
    now = now or timezone.now()
    cutoff = now - dt.timedelta(days=older_than_days)
    return CarQuestion.objects.filter(
        answered_at__isnull=True, is_published=False, created_at__lt=cutoff
    ).delete()
