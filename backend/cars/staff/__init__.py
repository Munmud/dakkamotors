"""Server-rendered staff pages, replacing the Django admin entity by entity.

`django.contrib.admin` is built on `QuerySet` and `ModelForm`, so every entity that moves
to DynamoDB loses its admin page on the way. There is no adapter and no partial
migration: the page has to exist before the entity can move, which is why these are being
built alongside the store rather than after it.

**Authentication is deliberately still Django's.** These use `staff_member_required`
today, because `auth_user` is still in Postgres and swapping identity is a separate
piece of work. When Cognito lands, only the decorator in `auth.py` changes -- the views,
forms and templates do not know or care how the person signed in.

Design decisions carried over from the admin classes these replace:

* No bulk tick-box edits. `ModelAdmin.get_changelist_form()` never passed
  `ModelAdmin.form`, so a tick-box on the changelist went around the form's validation
  entirely. Explicit buttons that say what they will do are both safer and clearer.
* Actions route through the domain modules (`qa.record_answer`, `booking.confirm_booking`)
  rather than writing state directly, so the email and the bell entry cannot be skipped
  by reaching the page a different way.
"""
