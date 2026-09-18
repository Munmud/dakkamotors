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

    direct_upload_fields = {"video": "video_key"}

    brand = forms.CharField(max_length=60)
    model_name = forms.CharField(max_length=80, label="Model")
    grade = forms.CharField(max_length=60, required=False)
    model_code = forms.CharField(max_length=40, required=False)
    chassis_number = forms.CharField(max_length=40)
    manufacture_year = forms.IntegerField(min_value=1900, max_value=2100, label="Year")

    fuel_type = forms.ChoiceField(choices=FuelType.choices, initial=FuelType.PETROL)
    seat_capacity = forms.IntegerField(min_value=1, max_value=20, initial=5)
    color = forms.CharField(max_length=40, required=False)

    price_jpy = forms.IntegerField(
        required=False, min_value=0, label="Price (JPY)",
        help_text="Leave blank to display 'Call for price' instead of an amount.",
    )
    status = forms.ChoiceField(choices=CarStatus.choices, initial=CarStatus.AVAILABLE)

    video = forms.FileField(
        required=False,
        help_text=(
            "Optional MP4 walkaround. Nothing downloads until a visitor presses play, "
            "so it costs nothing on page load. MP4 only - iPhone 'High Efficiency' "
            "clips are HEVC/.mov and will not play in Chrome or Firefox."
        ),
    )

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
        ("Vehicle", ["brand", "model_name", "grade", "model_code", "chassis_number",
                     "manufacture_year"]),
        ("Specification", ["fuel_type", "seat_capacity", "color"]),
        ("Listing", ["price_jpy", "status"]),
        ("Video", ["video"]),
        ("Description", ["description_en", "description_ja"]),
    )

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
    order = forms.IntegerField(required=False, initial=0, min_value=0)

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("image") and not self.uploaded_name("image"):
            if self._is_filled_in(cleaned):
                self.add_error("image", "Choose a photo, or clear this row.")
        return cleaned

    @staticmethod
    def _is_filled_in(cleaned):
        """True when the row carries intent, so blank extra rows stay ignorable."""
        return bool(cleaned.get("is_primary")) or bool(cleaned.get("order"))


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
