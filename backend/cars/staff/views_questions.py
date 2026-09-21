"""The queue of things buyers want to know.

Replaces `CarQuestionAdmin`. Every action routes through `qa.*` rather than writing the
item directly, which is what guarantees that answering emails the customer and that
publishing cannot happen without an answer -- reaching this page a different way cannot
skip either.
"""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .. import qa
from ..store import questions as store
from ..store import search
from .auth import requires, staff_required
from .forms import AnswerForm, QuestionFilterForm, QuestionTextForm


def _find(question_id):
    """Look one up by its public id.

    The sort key embeds the creation timestamp, so it cannot be reconstructed from the
    id alone. At this queue size scanning the (already single-Query) backlog is cheaper
    than carrying a second index just to make a URL shorter.
    """
    for question in store.queue():
        if question.question_id == question_id:
            return question
    raise Http404("No such question")


def _apply_state(questions, state):
    if state == "unanswered":
        return [q for q in questions if not q.is_answered]
    if state == "answered":
        return [q for q in questions if q.is_answered and not q.is_published]
    if state == "published":
        return [q for q in questions if q.is_published]
    return questions


@staff_required
@requires("question.view")
def question_list(request):
    form = QuestionFilterForm(request.GET or None)
    form.is_valid()
    data = form.cleaned_data if form.is_bound else {}

    questions = search.find_questions(
        term=data.get("q") or None,
        language=data.get("language") or None,
        brand=data.get("brand") or None,
    )
    questions = _apply_state(questions, data.get("state"))

    return render(request, "staff/questions/list.html", {
        "filtered": any(data.get(k) for k in ("q", "state", "language", "brand")),
        "title": "Questions",
        "form": form,
        "questions": questions,
        "waiting": sum(1 for q in store.queue() if not q.is_answered),
    })


@staff_required
@requires("question.view")
def question_detail(request, question_id):
    question = _find(question_id)

    if request.method == "POST":
        return _handle_action(request, question)

    return render(request, "staff/questions/detail.html", {
        "title": "Question",
        "question": question,
        "answer_form": AnswerForm(initial={"answer": question.answer or ""}),
        "question_form": QuestionTextForm(initial={"question": question.question}),
    })


def _handle_action(request, question):
    action = request.POST.get("action")
    here = reverse("staff:question-detail", args=[question.question_id])

    if action == "answer":
        return _answer(request, question, here)
    if action == "edit-question":
        return _edit_question(request, question, here)
    if action == "publish":
        return _publish(request, question, here)
    if action == "unpublish":
        return _unpublish(request, question, here)

    messages.error(request, "That button did not do anything. Try again, and if it keeps happening reload the page.")
    return redirect(here)


@requires("question.change")
def _answer(request, question, here):
    form = AnswerForm(request.POST)
    if not form.is_valid():
        messages.error(request, "That answer could not be saved.")
        return redirect(here)

    text = form.cleaned_data["answer"]
    was_answered = question.is_answered
    qa.record_answer(question, answer=text, staff=request.staff, now=timezone.now())

    if not text.strip():
        # Deliberately not an error: clearing an answer is how staff undo a mistake
        # before it is published. It just must never count as "answered".
        messages.warning(request, "Answer cleared. The customer has not been told.")
    elif was_answered:
        messages.info(request, "Answer updated. The customer was not emailed again.")
    else:
        messages.success(request, "Answer saved and emailed to the customer.")
    return redirect(here)


@requires("question.change")
def _edit_question(request, question, here):
    form = QuestionTextForm(request.POST)
    if not form.is_valid():
        messages.error(request, "That question could not be saved.")
        return redirect(here)
    store.edit_text(question, text=form.cleaned_data["question"], now=timezone.now())
    messages.success(request, "Question updated.")
    return redirect(here)


@requires("question.change")
def _publish(request, question, here):
    try:
        qa.publish(question, now=timezone.now())
    except qa.QuestionError as exc:
        # The message is written for the person reading it, so surface it verbatim.
        messages.error(request, str(exc))
        return redirect(here)
    messages.success(
        request,
        "Published on the car's page. The page is cached, so allow up to five minutes "
        "for it to appear.",
    )
    return redirect(here)


@requires("question.change")
def _unpublish(request, question, here):
    qa.unpublish(question, now=timezone.now())
    messages.success(request, "Removed from the car's public page.")
    return redirect(here)
