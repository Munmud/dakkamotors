"""Staff forms that accept a file already sitting in S3.

`direct-upload.js` sends the bytes to S3 before the form is submitted and writes the
resulting storage name into a hidden field. The form then carries that name instead of
receiving an upload.

The file inputs stay ordinary file inputs. If the JavaScript fails to load or errors,
the page still works through Django's normal upload path - just subject to the ~4.5 MB
ceiling that the direct upload exists to avoid.

Plain `Form`s since cars moved to DynamoDB; the mixin no longer writes onto a model
instance, it puts the storage name into `cleaned_data` for the view to use.
"""

from django import forms

from .choices import CarStatus, FuelType
from .specs import FIELDS as SPEC_FIELDS, MAX_LABEL, MAX_VALUE


class DirectUploadMixin:
    """Surface a posted S3 name alongside a file field's usual upload."""

    #: field name -> hidden field carrying the storage name
    direct_upload_fields = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, key_field in self.direct_upload_fields.items():
            self.fields[key_field] = forms.CharField(
                required=False, widget=forms.HiddenInput()
            )
            # The browser may satisfy this field by uploading to S3 instead, so it
            # cannot be required at the form level; clean() enforces it properly.
            self.fields[field_name].required = False

    def clean(self):
        cleaned = super().clean()
        for field_name, key_field in self.direct_upload_fields.items():
            key = (cleaned.get(key_field) or "").strip()
            if key:
                # The bytes are already in the bucket; the view stores the name.
                cleaned[f"{field_name}_direct_name"] = key
        return cleaned

    def uploaded_name(self, field_name):
        """The storage name for a field, whichever route the bytes arrived by."""
        return (self.cleaned_data.get(f"{field_name}_direct_name") or "").strip()


class CarForm(DirectUploadMixin, forms.Form):
    """Everything about one car.

    The field grouping, help text and wording are carried over from `CarAdminForm` and
    its fieldsets rather than reinvented - they were written for the person typing, and
    the storage change is no reason to make them re-learn the page.
    """

    # Nothing on this form uploads any more: the car's video moved to `CarVideoForm`,
    # a row in the gallery formset, when one video per car became many.
    direct_upload_fields = {}

    brand = forms.CharField(max_length=60)
    model_name = forms.CharField(max_length=80, label="Model")
    # No grade, model code or chassis number. Three boxes of trade paperwork at the top
    # of the page, two of them blank on most cars, ahead of the price and the photos.
    # Anything a particular car needs goes in Extra details, which is what that panel is
    # for and which puts it on the public spec table either way.
    #
    # The attributes stay on the model and legacy cars keep theirs, so the staff search
    # still finds an older car by part of its chassis number. They are simply no longer
    # something the form owns -- see EDITABLE in staff/views_cars.py, where leaving them
    # behind would have been a quiet data loss.
    manufacture_year = forms.IntegerField(min_value=1900, max_value=2100, label="Year")

    fuel_type = forms.ChoiceField(choices=FuelType.choices, initial=FuelType.PETROL)
    seat_capacity = forms.IntegerField(min_value=1, max_value=20, initial=5)
    color = forms.CharField(max_length=40, required=False)

    price_jpy = forms.IntegerField(
        required=False, min_value=0, label="Price (JPY)",
        help_text="Leave blank to display 'Call for price' instead of an amount.",
    )
    status = forms.ChoiceField(choices=CarStatus.choices, initial=CarStatus.AVAILABLE)

    description_en = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 6}),
        label="Description (English)",
        help_text=("Either language may be left blank — the site falls back to "
                   "whichever one is filled in."),
    )
    description_ja = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 6}),
        label="Description (Japanese)",
    )

    #: Rendered as groups, in this order. Same shape as the admin's fieldsets.
    GROUPS = (
        ("Vehicle", ["brand", "model_name", "manufacture_year"]),
        ("Specification", ["fuel_type", "seat_capacity", "color"]),
        ("Listing", ["price_jpy", "status"]),
        ("Description", ["description_en", "description_ja"]),
    )

    #: Fields whose box should be the width of their answer, not the width of the page.
    #: A four-digit year in a 34rem input reads as a text field waiting for a sentence.
    SHORT_FIELDS = ("manufacture_year", "seat_capacity", "price_jpy")

    def groups(self):
        for title, names in self.GROUPS:
            yield title, [self[name] for name in names]


class CarImageForm(DirectUploadMixin, forms.Form):
    """One photo row."""

    direct_upload_fields = {"image": "image_key"}

    image = forms.FileField(required=False)
    is_primary = forms.BooleanField(
        required=False,
        help_text="Shown on the listing card. Only the first one counts.",
    )
    # No `order`. Staff never type a position: a new photo goes on the end, and the
    # gallery on the edit page is reordered with Move up / Move down.
    #
    # Removed rather than hidden, and that is the point. A hidden field posting an
    # append index would make `_is_filled_in` below true for every untouched spare row,
    # so all three would fail with "Choose a photo, or clear this row." A hidden `0`
    # would be safe only because `bool(0)` is false -- a coincidence one `initial=`
    # away from breaking. With the field gone the trap cannot be re-armed.

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("image") and not self.uploaded_name("image"):
            if self._is_filled_in(cleaned):
                self.add_error("image", "Choose a photo, or clear this row.")
        return cleaned

    @staticmethod
    def _is_filled_in(cleaned):
        """True when the row carries intent, so blank extra rows stay ignorable.

        Ticking "card photo" on a row with no file is still a real mistake and still
        worth saying so about.
        """
        return bool(cleaned.get("is_primary"))


class CarVideoForm(DirectUploadMixin, forms.Form):
    """One video row, deliberately shaped like `CarImageForm`.

    The field is named `video` for a reason beyond symmetry: `direct-upload.js` decides
    the upload limit from the input's name, testing `/video/` before `/image/`, and
    derives the hidden field by replacing a trailing `video` with `video_key`. A formset
    prefixed `videos` therefore produces `videos-0-video`, which the existing script
    already signs at the 200 MB video cap with no change to it at all.

    No `is_primary` twin: a video can never be the listing card photo, which is the
    invariant `store/videos.py` exists to make structural.
    """

    direct_upload_fields = {"video": "video_key"}

    video = forms.FileField(
        required=False,
        help_text=(
            "MP4 only - iPhone 'High Efficiency' clips are HEVC/.mov and will not play "
            "in Chrome or Firefox. Nothing downloads until a visitor presses play."
        ),
    )
    # No `order`, and therefore no `clean`. `order` was this row's only signal that
    # somebody meant to fill it in -- there is no `is_primary` twin to fall back on --
    # so with it gone a blank video row is simply blank, and `_apply_videos` already
    # skips any row with no file.


class CarSpecForm(forms.Form):
    """One free-form detail: a label and a value, each in either language.

    `specs.clean` drops a half-filled row silently, because the importer writes through
    it with nobody watching. This form refuses one instead. The difference is deliberate:
    a staff member who typed "Colour" and tabbed away has made a mistake worth pointing
    at, whereas dropping it would look like the save had failed for no stated reason.

    An entirely blank row is still ignored, so the three spare rows on every page are
    not three errors.
    """

    label_en = forms.CharField(max_length=MAX_LABEL, required=False,
                               label="Label (English)")
    label_ja = forms.CharField(max_length=MAX_LABEL, required=False,
                               label="Label (Japanese)")
    value_en = forms.CharField(max_length=MAX_VALUE, required=False,
                               label="Value (English)")
    value_ja = forms.CharField(max_length=MAX_VALUE, required=False,
                               label="Value (Japanese)")

    def clean(self):
        cleaned = super().clean()
        if not any((cleaned.get(name) or "").strip() for name in SPEC_FIELDS):
            return cleaned
        if not (cleaned.get("label_en") or cleaned.get("label_ja")):
            self.add_error("label_en", "Name this detail, or clear the row.")
        if not (cleaned.get("value_en") or cleaned.get("value_ja")):
            self.add_error("value_en", "Give this detail a value, or clear the row.")
        return cleaned


class CarFilterForm(forms.Form):
    """`list_filter` and `search_fields`, as one GET form.

    `status` selects the index partition and costs nothing; the rest are Python
    predicates over what came back. See store/search.py for why, and for where the
    ceiling is.
    """

    q = forms.CharField(
        required=False, label="Search",
        widget=forms.TextInput(
            attrs={"placeholder": "brand, model, model code, chassis number"}),
    )
    status = forms.ChoiceField(
        required=False, label="Status",
        choices=[("", "Any status")] + list(CarStatus.choices),
    )
    fuel_type = forms.ChoiceField(
        required=False, label="Fuel",
        choices=[("", "Any fuel")] + list(FuelType.choices),
    )
    brand = forms.CharField(required=False, label="Brand")
