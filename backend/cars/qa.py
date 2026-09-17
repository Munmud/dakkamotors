"""The rules for asking and answering questions about a car.

Thin views, same as `booking.py`: every check lives here, so it applies whether the
request arrives from the app, from curl, from the staff pages, or from a test.

Two rules carry most of the weight:

**A question cannot be published without an answer.** This used to be enforced three
times over, and the docstring said so: a form's `clean()`, `publish()` here, and a
database `CheckConstraint` that was "the one that actually holds" because the admin
changelist bypassed form validation. DynamoDB has no table-level check, so it is now
enforced **twice** - here, for a readable message, and as a `ConditionExpression` on the
write in `store/questions.py`, which is the only module permitted to set the flag. A test
asserts that exclusivity mechanically. This is a real reduction in guarantee and it is
written down rather than glossed.

**A customer is emailed once, when their question is first answered.** Not every time the
answer is saved. Staff fix typos, and nobody wants three emails about one sentence. The
row lock that used to guarantee this is now a conditional write on `answered_at`, which
is the same guarantee without holding anything across a round trip.
"""

import datetime as dt

from django.utils import timezone

from . import identity
from . import mail
from . import notifications
from .choices import NotificationKind
from .store import cars as car_store
from .store import questions as store
from .store.errors import ConditionFailed

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

    Returns the stored question. The staff email is sent after the write returns rather
    than on transaction commit: a DynamoDB write either succeeds or raises, so "the call
    returned" *is* the commit point, and there is no later rollback for a message to be
    wrong about.
    """
    text = (question or "").strip()
    if not text:
        raise QuestionError("Please type your question first.")
    if len(text) > MAX_QUESTION_LENGTH:
        raise QuestionError(
            f"Please keep your question under {MAX_QUESTION_LENGTH} characters. "
            "If there is a lot to go through, call us and we will talk it over."
        )

    sub = identity.sub_of(user)
    if sub and store.open_count(sub) >= MAX_OPEN_QUESTIONS:
        raise QuestionError(
            f"You have {MAX_OPEN_QUESTIONS} questions with us already. "
            "We will answer those first."
        )

    record = store.create(
        car_id=identity.car_id_of(car),
        car_brand=car.brand,
        car_model_name=car.model_name,
        car_slug=car.slug,
        car_label=str(car),
        customer_sub=sub,
        customer_email=identity.email_of(user),
        question=text,
        language="ja" if language == "ja" else "en",
        now=timezone.now(),
    )
    mail.notify_staff_of_question(record, user)
    return record


def record_answer(question, *, answer, staff=None, now=None):
    """Save an answer, and if it is the first one, tell the customer.

    `answered_at` is what "has been answered" means, not a non-empty answer field. That
    is the difference between fixing a typo and sending someone their third email about
    the same sentence. Exactly one caller can win the conditional write, so a
    double-submitted form, or two staff racing each other, still produces one email.
    """
    now = now or timezone.now()
    question, first_answer = store.record_answer(
        question=question,
        answer=answer,
        staff_sub=identity.sub_of(staff),
        now=now,
    )

    if not first_answer:
        return question

    customer = identity.user_for_sub(question.customer_sub)
    if customer is None:
        # A staff-seeded question has no asker, and a closed account has nobody to tell.
        return question

    notifications.notify(
        user=customer,
        kind=NotificationKind.QUESTION_ANSWERED,
        context={
            "car_label": question.car_label or "",
            "car_slug": question.car_slug or "",
            "question_id": question.question_id,
        },
        dedupe_key=f"question:{question.question_id}:answered",
        now=now,
    )
    mail.notify_customer_of_answer(question, customer)
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

    try:
        # The question and the car's lastmod go in one transaction: a pair on the page
        # with a stale lastmod is a pair crawlers have no reason to come back for.
        question = store.publish(question, now=now)
    except ConditionFailed as exc:
        # The storage-layer guard disagreed with the check above, which means the answer
        # was emptied between the two. Same sentence either way.
        raise QuestionError(
            "Write an answer before publishing. A published question with no answer is "
            "worse than no question at all."
        ) from exc

    return question


def unpublish(question, *, now=None):
    now = now or timezone.now()
    question = store.unpublish(question, now=now)
    _touch_car(question, now)
    return question


def _touch_car(question, now):
    """Move the car's sitemap lastmod.

    New content on a page that still reports last year's date is new content a crawler
    has no reason to come back for.
    """
    try:
        car_store.bump_updated_at(question.car_id, now)
    except Exception:  # noqa: BLE001 - a missing car must not fail the publish
        pass


def published_for(car):
    """The pairs a visitor may see, newest first.

    The `Prefetch` dance this replaced existed to keep a page render at one query
    however many pairs a car had. That is now structural rather than something a caller
    has to remember: questions live in the car's own partition, so `store.cars.detail()`
    already carried them back with the car and attaches them as `questions`. This reads
    that when it is there, and falls back to its own query when it is not.
    """
    prefetched = getattr(car, "questions", None)
    # A Django Car also has a `questions` attribute -- its reverse related manager --
    # which is truthy and not iterable in the way this needs. Only a real list is the
    # store's attached thread.
    if isinstance(prefetched, list) and prefetched:
        return [q for q in prefetched if q.is_published]
    return store.published_for(identity.car_id_of(car))


def visible_in(questions, language):
    """Pairs written in the language the page is being served in.

    A question asked in Japanese and answered in Japanese is noise on the English page.
    But an empty section is worse than a wrong-language one, so if filtering leaves
    nothing, everything published is shown instead.
    """
    matching = [q for q in questions if q.language == language]
    return matching or list(questions)


def sweep_unanswered(older_than_days=180, now=None):
    """Housekeeping for the management command, never on a timer."""
    now = now or timezone.now()
    cutoff = now - dt.timedelta(days=older_than_days)
    doomed = [q for q in store.unanswered_before(cutoff) if not q.is_published]
    for question in doomed:
        question.delete()
    return len(doomed)
