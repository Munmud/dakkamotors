"""Plain forms for the staff pages.

`forms.Form`, never `ModelForm`: there is no model behind a DynamoDB item, and a
ModelForm's whole job is to map one onto fields. Validation that used to live on the
model moves here or into the store, and where it moved is stated on each form.
"""

from django import forms

from ..choices import BookingStatus, QuestionLanguage, Weekday


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


class BookingFilterForm(forms.Form):
    """The booking queue's filters.

    `status` used to be `list_filter`; `q` used to be `search_fields`. Phone search is
    deliberately gone: it reached through `customer__customer_profile__phone`, two joins
    for something staff do by name or email anyway.
    """

    q = forms.CharField(
        required=False, label="Search",
        widget=forms.TextInput(attrs={"placeholder": "name, email, car"}),
    )
    status = forms.ChoiceField(
        required=False, label="Status",
        choices=[("", "Pending and confirmed")] + list(BookingStatus.choices),
    )


class SlotFilterForm(forms.Form):
    """A from/to pair, replacing the admin's `date_hierarchy`.

    Two dates are what staff actually used the drill-down for, and they map straight
    onto a range query over the slot index rather than onto a year/month/day walk.
    """

    start = forms.DateField(
        required=False, label="From",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    end = forms.DateField(
        required=False, label="To",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    only_open = forms.BooleanField(required=False, label="Open only")


class ScheduleForm(forms.Form):
    """One recurring weekly rule.

    Descriptions carried over from `TestDriveScheduleAdmin`'s fieldsets: they explain
    consequences staff cannot see from the field names, which is exactly what help text
    is for.
    """

    weekday = forms.ChoiceField(choices=Weekday.choices, label="Weekday")
    start_time = forms.TimeField(
        label="From", widget=forms.TimeInput(attrs={"type": "time"}),
        help_text="Times are Japan local time. A rule repeats every week.",
    )
    end_time = forms.TimeField(
        label="To", widget=forms.TimeInput(attrs={"type": "time"}),
    )
    capacity = forms.IntegerField(
        min_value=1, initial=1, label="How many at once",
        help_text="How many customers can take this appointment at once - set 2 if two "
                  "cars or two staff are free.",
    )
    is_active = forms.BooleanField(
        required=False, initial=True, label="Active",
        help_text="Untick to stop generating new slots. Slots already created keep "
                  "their bookings; close them individually under Slots.",
    )
    starts_on = forms.DateField(
        required=False, label="Starts on",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Optional. Leave blank to start immediately.",
    )
    ends_on = forms.DateField(
        required=False, label="Ends on",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Optional. Leave blank to run indefinitely.",
    )
    note = forms.CharField(max_length=200, required=False, label="Note")

    def clean(self):
        cleaned = super().clean()
        starts, ends = cleaned.get("starts_on"), cleaned.get("ends_on")
        if starts and ends and ends < starts:
            self.add_error("ends_on", "The end date cannot be before the start date.")
        return cleaned
