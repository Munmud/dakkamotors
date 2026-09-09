"""Admin forms that accept a file already sitting in S3.

`direct-upload.js` sends the bytes to S3 before the form is submitted and writes the
resulting storage name into a hidden field. The form then points the FileField at that
name instead of receiving an upload.

The file inputs stay ordinary file inputs. If the JavaScript fails to load or errors,
the admin still works through Django's normal upload path - just subject to the ~4.5 MB
ceiling that the direct upload exists to avoid.
"""

from django import forms

from .models import Car, CarImage


class DirectUploadMixin:
    """Swap a posted S3 name in for a FileField's usual uploaded file."""

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
                # Assigning .name marks the field dirty without re-uploading anything,
                # which is the whole point: the bytes are already in the bucket.
                setattr(self.instance, f"{field_name}_direct_name", key)
                cleaned[field_name] = None
        return cleaned

    def _apply_direct_names(self, instance):
        for field_name in self.direct_upload_fields:
            name = getattr(instance, f"{field_name}_direct_name", None)
            if name:
                getattr(instance, field_name).name = name

    def save(self, commit=True):
        instance = super().save(commit=False)
        self._apply_direct_names(instance)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class CarImageForm(DirectUploadMixin, forms.ModelForm):
    direct_upload_fields = {"image": "image_key"}

    class Meta:
        model = CarImage
        fields = ["image", "is_primary", "order"]

    def clean(self):
        cleaned = super().clean()
        has_file = cleaned.get("image") or getattr(self.instance, "image_direct_name", None)
        if not has_file and not self.instance.pk and self._is_filled_in(cleaned):
            self.add_error("image", "Choose a photo, or clear this row.")
        return cleaned

    @staticmethod
    def _is_filled_in(cleaned):
        """True when the row carries intent, so blank extra rows stay ignorable."""
        return bool(cleaned.get("is_primary")) or bool(cleaned.get("order"))


class CarAdminForm(DirectUploadMixin, forms.ModelForm):
    direct_upload_fields = {"video": "video_key"}

    class Meta:
        model = Car
        fields = "__all__"
