"""Server-rendered staff pages, replacing the Django admin.

`django.contrib.admin` is built on `QuerySet` and `ModelForm`, so every entity that moved
to DynamoDB lost its admin page on the way. There was no adapter and no partial
migration: the page had to exist before the entity could move, which is why these were
built alongside the store rather than after it.

**Authentication is Cognito's hosted UI, isolated in `auth.py`.** The views, forms and
templates do not know or care how the person signed in -- which is what let these pages
be built before Cognito landed, and what made swapping the credential a one-module
change when it did.

Design decisions carried over from the admin classes these replace:

* No bulk tick-box edits. `ModelAdmin.get_changelist_form()` never passed
  `ModelAdmin.form`, so a tick-box on the changelist went around the form's validation
  entirely. Explicit buttons that say what they will do are both safer and clearer.
* Actions route through the domain modules (`qa.record_answer`, `booking.confirm_booking`)
  rather than writing state directly, so the email and the bell entry cannot be skipped
  by reaching the page a different way.
"""
