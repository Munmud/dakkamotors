"""Everything that talks to Cognito, in one module.

Kept narrow on purpose. The rest of the app sees `CognitoUser` and a handful of verbs;
it never learns what an attribute list looks like or that `custom:phone` has a prefix.

**Cognito never sends an email here.** The pool is configured with no auto-verified
attributes, admin-only account recovery and no `EmailConfiguration`, so it has no trigger
to fire. Verification and password-reset messages keep going out through
`mail.queue_email` -> S3 -> Brevo, bilingual and branded, as they always have. That is
the decision that makes Cognito adoptable here: the alternative is either its plain
English default mail against a 50/day cap, or SES -- and SES is where the previous
attempt at this stalled (see docs/TODO.md).

`custom:phone`, not the standard `phone_number`: Cognito validates the standard one as
E.164, and customers type `080-9282-3601`. Using it would have rejected the existing data
and made phone a sign-in alias, neither of which is wanted.
"""

import functools
import logging

import boto3
from botocore.config import Config
from django.conf import settings

logger = logging.getLogger(__name__)

#: Groups. `staff` is what `is_staff` was; `owners` is `is_superuser`.
STAFF_GROUP = "staff"
OWNERS_GROUP = "owners"
INVENTORY_GROUP = "inventory-managers"

PHONE_ATTR = "custom:phone"


class CognitoError(Exception):
    """Something Cognito refused, in words the caller can act on."""


@functools.lru_cache(maxsize=1)
def client():
    """One client per warm container.

    Rebuilding it per request would re-resolve credentials and re-open TLS every time,
    which on Lambda is most of the latency budget for a sign-in.
    """
    return boto3.client(
        "cognito-idp",
        region_name=settings.AWS_S3_REGION_NAME,
        endpoint_url=getattr(settings, "COGNITO_ENDPOINT_URL", "") or None,
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def reset_client():
    """Drop the cached client. Used by tests that install a mock backend."""
    client.cache_clear()


def _pool():
    return settings.COGNITO_POOL_ID


def _customer_client():
    return settings.COGNITO_CUSTOMER_CLIENT_ID


# --------------------------------------------------------------------------------------
# The user object the rest of the app sees
# --------------------------------------------------------------------------------------

class CognitoUser:
    """What `request.user` becomes.

    Exposes the attribute names Django's user had wherever something already reads them
    -- `is_authenticated`, `is_staff`, `get_full_name()`, `username`, `pk` -- so
    `IsAdminUser`, `UserRateThrottle` and most of `mail.py` keep working untouched.
    `pk` is the Cognito sub, which is what the store keys everything on.
    """

    is_anonymous = False
    is_authenticated = True

    def __init__(self, *, sub, email=None, first_name=None, last_name=None, phone=None,
                 groups=(), is_active=True, client_id=""):
        self.sub = sub
        self.groups = tuple(groups)
        self.is_active = is_active
        self.client_id = client_id
        self._attrs = None
        if email is not None or first_name is not None or phone is not None:
            self._attrs = {
                "email": email or "", "given_name": first_name or "",
                "family_name": last_name or "", PHONE_ATTR: phone or "",
            }

    # -- profile attributes, fetched only if something asks -------------------------
    #
    # An access token carries `sub`, `client_id` and `cognito:groups` and nothing else:
    # with email as the username attribute, Cognito's `username` claim is the sub, not
    # the address. So a page that wants a name or a phone number costs one AdminGetUser,
    # and the hot paths -- booking, asking, the bell -- pay nothing because they need
    # only the sub.

    def _load(self):
        if self._attrs is None:
            self._attrs = attributes_of(self.sub) or {}
        return self._attrs

    @property
    def email(self):
        return self._load().get("email", "")

    @property
    def first_name(self):
        return self._load().get("given_name", "")

    @property
    def last_name(self):
        return self._load().get("family_name", "")

    @property
    def phone(self):
        return self._load().get(PHONE_ATTR, "")

    # -- the shapes other code already expects ------------------------------------

    @property
    def pk(self):
        return self.sub

    @property
    def id(self):
        return self.sub

    @property
    def username(self):
        return self.email

    @property
    def is_staff(self):
        return STAFF_GROUP in self.groups

    @property
    def is_superuser(self):
        return OWNERS_GROUP in self.groups

    def get_full_name(self):
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()

    def get_short_name(self):
        return self.first_name or self.email

    def has_perm(self, *args, **kwargs):
        """Permissions are groups now.

        Deliberately always False rather than approximating: `cars/staff/auth.py` owns
        the group -> action map, and a half-working `has_perm` here would let a caller
        think it had asked a real question.
        """
        return False

    def __str__(self):
        return self.email or self.sub

    @classmethod
    def from_claims(cls, claims):
        """Everything the token actually carries, and nothing invented.

        Note what is *not* here: the email. Deriving it from the `username` claim would
        look right and be a UUID.
        """
        return cls(
            sub=claims["sub"],
            groups=claims.get("cognito:groups", ()),
            client_id=claims.get("client_id", ""),
        )


# --------------------------------------------------------------------------------------
# Sign-up and confirmation
# --------------------------------------------------------------------------------------

def sign_up(*, email, password, name="", phone=""):
    """Create an UNCONFIRMED user. They cannot sign in until confirmed.

    Cognito holds the password from this moment, so nothing here ever stores one -- a
    real improvement on the table this replaces, which carried a hash for three days.
    """
    first, _, last = (name or "").partition(" ")
    attributes = [{"Name": "email", "Value": email}]
    if first:
        attributes.append({"Name": "given_name", "Value": first[:150]})
    if last:
        attributes.append({"Name": "family_name", "Value": last[:150]})
    if phone:
        attributes.append({"Name": PHONE_ATTR, "Value": phone[:32]})

    try:
        client().sign_up(ClientId=_customer_client(), Username=email,
                         Password=password, UserAttributes=attributes)
    except client().exceptions.UsernameExistsException:
        raise
    except client().exceptions.InvalidPasswordException as exc:
        raise CognitoError(_password_message(exc)) from exc


def _password_message(exc):
    detail = exc.response.get("Error", {}).get("Message", "")
    return detail or "That password is not strong enough."


def status_of(email):
    """UNCONFIRMED / CONFIRMED / ..., or None if there is no such user."""
    try:
        return client().admin_get_user(
            UserPoolId=_pool(), Username=email)["UserStatus"]
    except client().exceptions.UserNotFoundException:
        return None


def confirm(email):
    client().admin_confirm_sign_up(UserPoolId=_pool(), Username=email)
    client().admin_update_user_attributes(
        UserPoolId=_pool(), Username=email,
        UserAttributes=[{"Name": "email_verified", "Value": "true"}],
    )


def delete_user(email):
    try:
        client().admin_delete_user(UserPoolId=_pool(), Username=email)
    except client().exceptions.UserNotFoundException:
        pass


# --------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------

def authenticate(*, email, password):
    """Sign in. Returns the token bundle, or None if the credentials are wrong.

    ADMIN_USER_PASSWORD_AUTH rather than USER_PASSWORD_AUTH: it requires signed AWS
    credentials, so a leaked client id cannot be used to brute-force from a browser and
    every attempt goes through Django, where the throttle still applies.
    """
    try:
        response = client().admin_initiate_auth(
            UserPoolId=_pool(), ClientId=_customer_client(),
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )
    except (client().exceptions.NotAuthorizedException,
            client().exceptions.UserNotFoundException,
            client().exceptions.UserNotConfirmedException):
        return None
    return response.get("AuthenticationResult")


def refresh(refresh_token):
    try:
        response = client().admin_initiate_auth(
            UserPoolId=_pool(), ClientId=_customer_client(),
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": refresh_token},
        )
    except Exception:  # noqa: BLE001 - any refusal means "sign in again"
        return None
    return response.get("AuthenticationResult")


def sign_out(email):
    try:
        client().admin_user_global_sign_out(UserPoolId=_pool(), Username=email)
    except Exception:  # noqa: BLE001 - logging out must always appear to work
        logger.info("global sign-out failed for %s", email, exc_info=True)


# --------------------------------------------------------------------------------------
# Attributes and groups
# --------------------------------------------------------------------------------------

def attributes_of(email):
    try:
        user = client().admin_get_user(UserPoolId=_pool(), Username=email)
    except client().exceptions.UserNotFoundException:
        return None
    return {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}


def update_attributes(username, **fields):
    """Set given_name / family_name / phone. Email is deliberately not settable.

    The target is `username` rather than `email` so that passing `email=` as a field to
    change cannot collide with it -- and because Cognito's own APIs call it that, and it
    accepts a sub there just as happily.

    Refusing `email` is a security control, not a UI nicety: the address *is* the
    username, so moving one without the other would let a customer take an address that
    already belongs to somebody else.
    """
    mapping = {"first_name": "given_name", "last_name": "family_name",
               "phone": PHONE_ATTR}
    attributes = [{"Name": mapping[k], "Value": (v or "")[:150]}
                  for k, v in fields.items() if k in mapping]
    if attributes:
        client().admin_update_user_attributes(
            UserPoolId=_pool(), Username=username, UserAttributes=attributes)


def set_password(email, password, *, permanent=True):
    try:
        client().admin_set_user_password(
            UserPoolId=_pool(), Username=email, Password=password,
            Permanent=permanent)
    except client().exceptions.InvalidPasswordException as exc:
        raise CognitoError(_password_message(exc)) from exc


def groups_of(email):
    try:
        response = client().admin_list_groups_for_user(
            UserPoolId=_pool(), Username=email)
    except client().exceptions.UserNotFoundException:
        return []
    return [g["GroupName"] for g in response.get("Groups", [])]


def add_to_group(email, group):
    client().admin_add_user_to_group(UserPoolId=_pool(), Username=email,
                                     GroupName=group)


def remove_from_group(email, group):
    client().admin_remove_user_from_group(UserPoolId=_pool(), Username=email,
                                          GroupName=group)


def users_in_group(group):
    """Everyone in a group.

    `ListUsers` cannot filter by group, so this is the only way -- and it paginates at
    60, which is worth knowing rather than discovering.
    """
    out, token = [], None
    while True:
        kwargs = {"UserPoolId": _pool(), "GroupName": group, "Limit": 60}
        if token:
            kwargs["NextToken"] = token
        response = client().list_users_in_group(**kwargs)
        out.extend(response.get("Users", []))
        token = response.get("NextToken")
        if not token:
            return out


def user_for_sub(sub):
    """Rebuild a CognitoUser from a subject identifier.

    Works because with `UsernameAttributes: [email]` a pool's internal username *is* the
    sub, so every admin API accepts one wherever it asks for a Username. That is worth
    knowing: it is the reason nothing here has to keep a sub -> email index.
    """
    attrs = attributes_of(sub)
    if attrs is None:
        return None
    return CognitoUser(
        sub=attrs.get("sub", sub), email=attrs.get("email", ""),
        first_name=attrs.get("given_name", ""),
        last_name=attrs.get("family_name", ""),
        phone=attrs.get(PHONE_ATTR, ""), groups=groups_of(sub),
    )
