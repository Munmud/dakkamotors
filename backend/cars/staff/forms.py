"""Plain forms for the staff pages.

`forms.Form`, never `ModelForm`: there is no model behind a DynamoDB item, and a
ModelForm's whole job is to map one onto fields. Validation that used to live on the
model moves here or into the store, and where it moved is stated on each form.
"""

from django import forms

from ..cognito import INVENTORY_GROUP, OWNERS_GROUP, STAFF_GROUP
from .permissions import ASSIGNABLE_GROUPS

#: Human wording for the group names Cognito stores.
GROUP_LABELS = {
    OWNERS_GROUP: "Owner",
    STAFF_GROUP: "Staff",
    INVENTORY_GROUP: "Inventory manager",
}


class StaffAccountForm(forms.Form):
    """Add or edit a staff account.

    The group list offers `ASSIGNABLE_GROUPS` and nothing else. That is the whole
    escalation defence on this form: `owners` is not a choice, so it cannot be granted
    here however the POST is shaped -- a value outside the choices fails validation
    rather than being quietly ignored.
    """

    email = forms.EmailField(
        label="Email address",
        help_text="Also the sign-in name. It cannot be changed afterwards.",
    )
    name = forms.CharField(label="Full name", max_length=120, required=False)
    groups = forms.MultipleChoiceField(
        label="Roles", required=False, widget=forms.CheckboxSelectMultiple,
        help_text="Everyone here can sign in to these pages. A role adds what they may "
                  "change.",
    )
    is_active = forms.BooleanField(
        label="Active", required=False, initial=True,
        help_text="Deactivating keeps the account and its history; it only stops the "
                  "sign-in.",
    )

    def __init__(self, *args, editing_self=False, existing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.editing_self = editing_self
        self.fields["groups"].choices = [
            (group, GROUP_LABELS.get(group, group)) for group in ASSIGNABLE_GROUPS
        ]
        if existing:
            # The address is the Cognito username, and a pool will not rename one.
            self.fields["email"].disabled = True
            self.fields["email"].help_text = "Sign-in name. It cannot be changed."

    def clean_is_active(self):
        active = self.cleaned_data["is_active"]
        # One careless tick would otherwise lock you out of your own account with no way
        # back in short of the AWS console.
        if self.editing_self and not active:
            raise forms.ValidationError("You cannot deactivate your own account.")
        return active


class RequestFilterForm(forms.Form):
    """Open, resolved, or everything. There is nothing else worth filtering a lead by."""

    STATE_CHOICES = [
        ("open", "Open"),
        ("resolved", "Resolved"),
        ("", "Everything"),
    ]

    state = forms.ChoiceField(required=False, choices=STATE_CHOICES, label="Show")


class ResolveRequestForm(forms.Form):
    """What was done about it, for whoever reads the list next."""

    note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="What happened",
        help_text="Optional. \"Found them a 2019 Tanto\" or \"No longer looking\" is plenty.",
    )
