"""Plain forms for the staff pages.

`forms.Form`, never `ModelForm`: there is no model behind a DynamoDB item, and a
ModelForm's whole job is to map one onto fields. Validation that used to live on the
model moves here or into the store, and where it moved is stated on each form.
"""

from django import forms

from ..choices import QuestionLanguage


class AnswerForm(forms.Form):
    """Write or correct an answer.

    Saving an answer emails the customer straight away. Editing one already sent does
    not email them again -- that is decided by `answered_at` in the store, not here.
    """

    answer = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 8, "class": "staff-textarea"}),
        required=False,
        label="Your answer",
        help_text=(
            "Saving an answer emails the customer straight away. Editing one you have "
            "already sent does not email them again."
        ),
    )


class QuestionTextForm(forms.Form):
    """Tidy the question before it goes public.

    Editable on purpose: people type phone numbers and their own names into free text,
    and a published question goes on a public page and into search results.
    """

    question = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4, "class": "staff-textarea"}),
        label="The question",
        help_text=(
            "People type their phone number and their name into free text, and this "
            "goes on a public page - tidy it before you publish."
        ),
    )


class QuestionFilterForm(forms.Form):
    """The changelist's filters and search, as one GET form.

    `status` and `language` used to be `list_filter`; `q` used to be `search_fields`.
    All three are applied in Python over one Query -- see store/search.py for why that
    is the right call at this data size, and where the ceiling is.
    """

    STATE_CHOICES = [
        ("", "Any state"),
        ("unanswered", "Needs an answer"),
        ("answered", "Answered, not public"),
        ("published", "Published"),
    ]

    q = forms.CharField(
        required=False, label="Search",
        widget=forms.TextInput(attrs={"placeholder": "question, answer, car, email"}),
    )
    state = forms.ChoiceField(required=False, choices=STATE_CHOICES, label="State")
    language = forms.ChoiceField(
        required=False, label="Language",
        choices=[("", "Any language")] + list(QuestionLanguage.choices),
    )
    brand = forms.CharField(required=False, label="Brand")
