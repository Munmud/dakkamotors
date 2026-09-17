"""Question and answer storage.

**This module is the only thing allowed to write `is_published`.** That is not a style
preference, it is the compensation for a guarantee the migration loses.

`qa_models.py` enforced "a published question has an answer" with a database
`CheckConstraint`, and its docstring is explicit that the constraint -- not the form -- is
"the guard that actually holds", because the admin changelist bypasses `ModelForm.clean()`.
DynamoDB has no table-level check. A `ConditionExpression` on the publish write is as
close as it gets, and it only guards *that write*: any other code path that set the flag
would sail past it.

So the guarantee is now: one writer, one condition, and a test asserting no other module
touches the attribute. Weaker than a constraint, and worth knowing it.
"""

import datetime as dt

from ..choices import QuestionLanguage
from . import keys
from .errors import ConditionFailed, NotFound
from .models import Car, CarQuestion
from .txn import Txn


def create(*, car_id, car_brand, car_model_name, car_slug, car_label,
           customer_sub, customer_email, question,
           language=QuestionLanguage.EN, now):
    """Record a question.

    Takes the car's fields rather than a car object on purpose: while the migration is
    in flight the caller may hold a Django Car or a store Car, and this module should
    not have to know or care which.
    """
    question_id = keys.new_id()
    row = CarQuestion(
        pk=keys.car_pk(car_id),
        sk=keys.question_sk(now, question_id),
        question_id=question_id,
        car_id=car_id,
        # Denormalised so the staff queue can filter and search by car with no join.
        car_brand=car_brand,
        car_model_name=car_model_name,
        car_slug=car_slug,
        car_label=car_label,
        customer_sub=customer_sub,
        customer_email=customer_email,
        question=question,
        answer="",
        is_published=False,
        language=language,
        created_at=now,
        updated_at=now,
        gsi1pk=keys.QUESTION_GSI1PK,
        gsi1sk=keys.question_gsi1sk(now, car_id, question_id),
        gsi2pk=keys.customer_pk(customer_sub) if customer_sub else None,
        gsi2sk=f"Q#{keys.iso(now)}#{car_id}#{question_id}" if customer_sub else None,
    )
    row.search_blob = row.build_search_blob()
    row.save()
    return row


def get(car_id, sk):
    try:
        return CarQuestion.get(keys.car_pk(car_id), sk)
    except CarQuestion.DoesNotExist:
        raise NotFound(f"no question {sk!r} on car {car_id!r}") from None


def published_for(car_id):
    """Published pairs for one car, newest first.

    Normally this needs no request of its own -- `cars.detail()` already carried the
    questions back in the same Query as the car. Kept for the paths that only want the
    thread.
    """
    rows = CarQuestion.query(keys.car_pk(car_id),
                            range_key_condition=CarQuestion.sk.startswith("Q#"),
                            scan_index_forward=False)
    return [q for q in rows if q.is_published]


def for_customer(customer_sub, limit=50):
    rows = list(CarQuestion.gsi2.query(keys.customer_pk(customer_sub),
                                       scan_index_forward=False))
    return rows[:limit]


def open_count(customer_sub):
    """How many of this customer's questions are still waiting on the shop."""
    return sum(1 for q in for_customer(customer_sub) if not q.is_answered)


def queue(limit=None):
    """Every question, newest first -- the staff backlog."""
    rows = list(CarQuestion.gsi1.query(keys.QUESTION_GSI1PK, scan_index_forward=False))
    return rows[:limit] if limit else rows


def unanswered_before(cutoff):
    return [q for q in queue()
            if not q.is_answered and (q.created_at or dt.datetime.max) < cutoff]


def record_answer(*, question, answer, staff_sub, now):
    """Write an answer, and say whether this was the *first* one.

    Replaces `select_for_update()`. The lock existed so that exactly one caller could
    conclude "I answered this" and send exactly one email. A conditional write does the
    same job without holding anything across a round trip: whoever wins the
    `answered_at.does_not_exist()` condition is the one that answers.

    Editing an answer months later takes the other branch and tells nobody, which is the
    behaviour `qa_models.py` describes.
    """
    text = (answer or "").strip()
    if not text:
        # An empty answer must never stamp answered_at -- that is what would silently
        # mark the question as done and stop the customer ever hearing back.
        question.update(actions=[
            CarQuestion.answer.set(""),
            CarQuestion.updated_at.set(now),
        ])
        return question, False

    actions = [
        CarQuestion.answer.set(text),
        CarQuestion.answered_at.set(now),
        CarQuestion.answered_by.set(staff_sub or ""),
        CarQuestion.updated_at.set(now),
    ]
    try:
        question.update(actions=actions,
                        condition=CarQuestion.answered_at.does_not_exist())
        question.refresh()
        _refresh_blob(question)
        return question, True
    except Exception as exc:
        if not _is_conditional_failure(exc):
            raise

    # Already answered: update the text, leave answered_at and the email alone.
    question.update(actions=[
        CarQuestion.answer.set(text),
        CarQuestion.updated_at.set(now),
    ])
    question.refresh()
    _refresh_blob(question)
    return question, False


def _refresh_blob(question):
    question.update(actions=[CarQuestion.search_blob.set(question.build_search_blob())])


def _is_conditional_failure(exc):
    cause = getattr(exc, "cause", None)
    code = (cause.response.get("Error", {}).get("Code")
            if cause is not None and hasattr(cause, "response") else None)
    return code == "ConditionalCheckFailedException"


def publish(question, *, now, bump_car=True):
    """Put the pair on the car's public page, and move the car's sitemap lastmod.

    The condition is the nearest thing left to the old CheckConstraint. It guards this
    write; it cannot guard the table. See the module docstring.

    `bump_car=False` while cars still live in Postgres: there is no DynamoDB car item to
    update yet, so the caller bumps the ORM row instead. Flip it back -- and delete the
    parameter -- when cars move, at which point the question and the car's lastmod go in
    one transaction again.
    """
    tx = Txn()
    tx.update(
        "question",
        CarQuestion(pk=question.pk, sk=question.sk),
        actions=[
            CarQuestion.is_published.set(True),
            CarQuestion.updated_at.set(now),
        ],
        condition=(CarQuestion.answer.exists() & (CarQuestion.answer != "")),
    )
    if bump_car:
        tx.update(
            "car",
            Car(pk=keys.car_pk(question.car_id), sk=keys.META),
            actions=[Car.updated_at.set(now)],
        )
    order = tx.labels_in_wire_order()
    try:
        tx.commit()
    except Exception as exc:
        from .txn import failed
        if getattr(exc, "reasons", None) is not None and failed(exc, "question", order):
            raise ConditionFailed(
                "a published question must have an answer") from exc
        raise
    question.is_published = True
    question.updated_at = now
    return question


def unpublish(question, *, now):
    question.update(actions=[
        CarQuestion.is_published.set(False),
        CarQuestion.updated_at.set(now),
    ])
    return question


def edit_text(question, *, text, now):
    """Staff tidying a question before it goes public.

    People type their phone number and their name into free text and this ends up on a
    public page, so the question itself stays editable.
    """
    question.update(actions=[
        CarQuestion.question.set(text),
        CarQuestion.updated_at.set(now),
    ])
    question.refresh()
    _refresh_blob(question)
    return question
