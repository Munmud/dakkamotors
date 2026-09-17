"""Who a customer is, during the move from Django auth to Cognito.

The store keys everything on a Cognito `sub`. While the ORM is still present, the app
hands the domain layer a Django `User`, whose identity is an integer primary key. This
module is the single bridge between the two, so that every other module can simply ask
for `sub_of(user)` and stop caring which world it is in.

It exists to be deleted. When Cognito lands, `sub_of` becomes `user.sub` and the
migration importer will have written the same value into every stored item -- which is
why the mapping has to be stable and defined in exactly one place rather than inlined at
a dozen call sites.
"""


def sub_of(user):
    """The store's identifier for a customer, or None if there is nobody.

    A Cognito-backed user carries its own `sub`. A Django user does not, so its primary
    key is used, stringified -- the migration importer writes the same value, so items
    written before and after the cutover agree.
    """
    if user is None:
        return None
    sub = getattr(user, "sub", None)
    if sub:
        return str(sub)
    pk = getattr(user, "pk", None)
    return str(pk) if pk is not None else None


def is_reachable(user):
    """Whether this person can receive anything at all.

    Staff-seeded questions have no asker, and a deactivated account has no bell. Both
    are ordinary states, not errors, so callers check rather than guard with try.
    """
    return user is not None and bool(getattr(user, "is_active", False))


def email_of(user):
    return (getattr(user, "email", "") or "").strip()


def phone_of(user):
    """The phone number, wherever it currently lives.

    Django keeps it on a one-to-one CustomerProfile; Cognito will keep it as a custom
    attribute read straight off the user. Both are handled so callers need not branch.
    """
    phone = getattr(user, "phone", None)
    if phone:
        return phone
    profile = getattr(user, "customer_profile", None)
    return getattr(profile, "phone", "") if profile else ""


def full_name_of(user):
    if user is None:
        return ""
    getter = getattr(user, "get_full_name", None)
    name = getter() if callable(getter) else ""
    return (name or getattr(user, "username", "") or "").strip()


def car_id_of(car):
    """The store's identifier for a car, from either representation.

    Same bridge as `sub_of`, for the same reason and with the same expiry date. A store
    Car carries `car_id`; a Django Car has an integer primary key, and the migration
    importer writes that same value as a string, so items written on either side of the
    cutover agree.
    """
    if car is None:
        return None
    car_id = getattr(car, "car_id", None)
    if car_id:
        return str(car_id)
    pk = getattr(car, "pk", None)
    return str(pk) if pk is not None else None


def user_for_sub(sub):
    """Resolve a stored subject identifier back to a person, or None.

    The other half of `sub_of`, and the other half of this module's expiry date. Today
    that is a Django user lookup by primary key; with Cognito it becomes an
    `AdminGetUser` call, and only this function changes.

    Returns None rather than raising: a question outlives the account that asked it by
    design (`CarQuestion.customer` was SET_NULL precisely so a closed account could not
    silently delete published page content), so "nobody to tell" is an ordinary answer.
    """
    if not sub:
        return None

    # A Cognito sub is a UUID; a Django primary key is an integer. Which one a stored
    # identifier is says which world it came from, so the shape is the lookup.
    try:
        pk = int(sub)
    except (TypeError, ValueError):
        from . import cognito

        return cognito.user_for(sub)

    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=pk).first()
