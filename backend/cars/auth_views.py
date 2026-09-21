"""Customer accounts, on Cognito.

Separate from staff entirely: a customer is simply not in the `staff` group, so the
staff pages turn them away on their own. Email is the sign-in name, which is what the
pool's `UsernameAttributes: [email]` means -- with the consequence, worth knowing, that
Cognito's *internal* username is then a UUID and the address is an attribute.

**No usable account exists until the address has been proved.** A sign-up creates an
UNCONFIRMED Cognito user, which cannot sign in, and stores the name and phone alongside
it; clicking the emailed link confirms it. That is why nothing downstream asks whether a
customer is verified -- an unverified person cannot get a token.

**Cognito sends nothing.** Every message here still goes out through `mail.queue_email`
-> S3 -> Brevo, bilingual and branded. Handing verification to Cognito would mean plain
English against a 50/day cap, or SES; see `cognito.py` for why neither is wanted.

**The link signs them in as well as verifying them.** Cognito holds the password from
sign-up and never hands it back, so for a while verification stopped at "confirmed" and
sent people to the sign-in form -- one more screen, and the page they had come from
lost along the way. The pool's custom auth flow closes that gap: `LinkAuthFunction`
issues a single challenge whose answer is the link token itself, and
`cognito.sign_in_with_link` answers it right after `confirm`. Holding the link already
proved the address; letting it open the session adds no secret and stores no password.

If that sign-in is refused -- the flow not yet enabled on the client, Cognito having a
bad minute -- verification still succeeds and the response says `signed_in: false`, and
the app falls back to the sign-in form. A confirmed account is never lost to a failed
convenience.
"""

import logging

from django.middleware.csrf import get_token
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from . import authentication, cognito, identity, mail, seo
from .store import auth as auth_store
from .store import customers as customer_store

logger = logging.getLogger(__name__)

#: Length alone, as before. A pool has exactly one password policy and it must stay this
#: permissive or an existing customer with a six-character password cannot be migrated.
MIN_PASSWORD_LENGTH = 6


class AuthThrottle(AnonRateThrottle):
    """Blunts credential stuffing, bulk sign-ups and inbox bombing.

    Matters more than usual: customers may choose weak passwords, so the rate limit is
    doing work that password complexity is not. It is also why every sign-in goes
    through Django rather than the browser talking to Cognito directly -- the customer
    app client has no password auth flow enabled at all.
    """

    scope = "auth"


def _me(user):
    return {
        "name": identity.full_name_of(user),
        "first_name": getattr(user, "first_name", "") or "",
        "last_name": getattr(user, "last_name", "") or "",
        "email": identity.email_of(user),
        "phone": identity.phone_of(user),
        # Drives the Admin button in the masthead. It is the viewer's own flag, so
        # telling them about it reveals nothing they could not discover by opening the
        # staff URL.
        "is_staff": bool(getattr(user, "is_staff", False)),
    }


def safe_next(value):
    """Only allow a path on this site.

    `//evil.com` and `https://evil.com` are both valid values for a browser to follow,
    so anything that is not a single leading slash is discarded rather than trusted.
    """
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//"):
        return ""
    return value[:200]


def _secure_cookies(request):
    """Secure cookies everywhere except plain-HTTP local development."""
    return request.is_secure()


class CsrfView(APIView):
    """Hands the app a CSRF token.

    Survives the move to Cognito untouched, which surprises people: CSRF protection
    needs no database and no session. With `CSRF_USE_SESSIONS = False` the token is a
    cookie-plus-secret construction, so it kept working when `django.contrib.sessions`
    left.

    It cannot come from the rendered page: those are cached at the CDN for five minutes
    with cookies ignored, so a token set there would be handed to every visitor alike.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"csrfToken": get_token(request)})


# --------------------------------------------------------------------------------------
# Registration - two steps, because the account is unusable until step two
# --------------------------------------------------------------------------------------


class RegisterSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=32)
    password = serializers.CharField(write_only=True)
    # `allow_null` as well as `allow_blank`, and the difference is the whole bug this
    # fixed. The form reads `next` from the query string, and `URLSearchParams.get`
    # returns null when the parameter is absent -- which axios then serialises as a
    # literal null. DRF skips a *missing* optional key but refuses an explicit null
    # unless told otherwise, so every registration that did not start from a booking
    # link was answered with "This field may not be null." about a field the form does
    # not show. `safe_next` already treated None as blank; the serializer just never let
    # it get that far.
    next = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    language = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate_email(self, value):
        return value.strip().lower()

    def validate_password(self, value):
        if len(value) < MIN_PASSWORD_LENGTH:
            raise serializers.ValidationError(
                f"This password is too short. It must contain at least "
                f"{MIN_PASSWORD_LENGTH} characters."
            )
        return value


class RegisterView(APIView):
    """Step one: create the unconfirmed account and email a link.

    Returns 202 and no session - there is nothing to sign in to yet.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        email = data["email"]

        existing = cognito.status_of(email)
        if existing == "CONFIRMED":
            # This does reveal that an account exists. Kept deliberately: a customer who
            # has forgotten needs telling, and a used-car customer list is not a secret.
            return Response(
                {"email": ["An account with that email already exists. "
                           "Try signing in instead."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if existing == "UNCONFIRMED":
            # A typo on the first attempt must not lock that address out. Replacing the
            # unconfirmed account is also how "register again" works.
            cognito.delete_user(email)

        try:
            cognito.sign_up(email=email, password=data["password"],
                            name=data["name"], phone=data["phone"])
        except cognito.CognitoError as exc:
            return Response({"password": [str(exc)]},
                            status=status.HTTP_400_BAD_REQUEST)

        language = "ja" if (data.get("language") or "").startswith("ja") else "en"
        raw_token = auth_store.start_registration(
            email=email, name=data["name"], phone=data["phone"],
            next_path=safe_next(data.get("next")), language=language,
            now=timezone.now(),
        )

        mail.send_verification_email(
            auth_store.find_registration(email),
            f"{seo.SITE_URL}/account/verify?token={raw_token}",
        )
        return Response(
            {"detail": "Check your email to finish creating your account.",
             "email": email},
            status=status.HTTP_202_ACCEPTED,
        )


class VerifyView(APIView):
    """Step two: the link. This is what makes the account usable."""

    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        raw_token = (request.data.get("token") or "").strip()
        if not raw_token:
            return Response({"detail": "That link is not valid."},
                            status=status.HTTP_400_BAD_REQUEST)

        pending = auth_store.registration_for_token(raw_token)
        if pending is None:
            return Response(
                {"detail": "That link has already been used, or is not valid. "
                           "Try signing in, or register again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if cognito.status_of(pending.email) is None:
            auth_store.finish_registration(pending)
            return Response(
                {"detail": "That sign-up has expired. Please register again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cognito.confirm(pending.email)
        attrs = cognito.attributes_of(pending.email) or {}
        # The counter the booking limit is enforced against, and the staff customer
        # list, both live here rather than in Cognito.
        customer_store.ensure(sub=attrs.get("sub", ""), email=pending.email,
                              now=timezone.now())
        next_path = pending.next_path

        # Before `finish_registration`, not after: the custom-auth trigger recognises
        # the token by reading the same pending item, so the order is the correctness.
        tokens = cognito.sign_in_with_link(email=pending.email, raw_token=raw_token)
        auth_store.finish_registration(pending)

        body = {
            "verified": True,
            "email": pending.email,
            "next": next_path or "/account",
            "signed_in": tokens is not None,
        }
        if tokens is None:
            return Response(body)

        user = (cognito.user_for(pending.email)
                or cognito.CognitoUser(sub=attrs.get("sub", ""), email=pending.email))
        body.update(_me(user))
        return authentication.set_session_cookies(
            Response(body), tokens, secure=_secure_cookies(request))


class ResendVerificationView(APIView):
    """Send the link again.

    Answers identically whether or not a pending sign-up exists, so it cannot be used to
    discover addresses or to bomb someone's inbox.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        pending = auth_store.find_registration(email)

        if pending is not None and cognito.status_of(email) == "UNCONFIRMED":
            raw_token = auth_store.start_registration(
                email=email, name=pending.name, phone=pending.phone,
                next_path=pending.next_path, language=pending.language,
                now=timezone.now(),
            )
            mail.send_verification_email(
                auth_store.find_registration(email),
                f"{seo.SITE_URL}/account/verify?token={raw_token}",
            )

        return Response(
            {"detail": "If that sign-up is waiting, we have sent the link again."},
            status=status.HTTP_202_ACCEPTED,
        )


# --------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        password = request.data.get("password") or ""

        tokens = cognito.authenticate(email=email, password=password)
        if not tokens:
            # One message for every failure - wrong password, no such account, not yet
            # confirmed - so the response cannot be used to discover which addresses
            # have accounts.
            return Response(
                {"detail": "Email or password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = cognito.user_for(email) or cognito.CognitoUser(sub=email, email=email)
        customer_store.ensure(sub=user.sub, email=user.email, now=timezone.now())

        response = Response(_me(user))
        return authentication.set_session_cookies(
            response, tokens, secure=_secure_cookies(request))


class LogoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        user = getattr(request, "user", None)
        if getattr(user, "is_authenticated", False) and hasattr(user, "sub"):
            # Revokes the refresh token pool-side, so a stolen one stops working rather
            # than living out its thirty days.
            cognito.sign_out(user.sub)

        response = Response(status=status.HTTP_204_NO_CONTENT)
        return authentication.clear_session_cookies(response)


# What a customer may change about themselves. Anything outside this set is refused
# rather than quietly dropped, so a caller is never left thinking a change took.
EDITABLE_PROFILE_FIELDS = {"first_name", "last_name", "phone"}

# Refusing `email` is a security control, not a UI nicety.
#
# The address *is* the sign-in name. Letting it move would let a customer take an
# address that already belongs to someone else, and a reset link would then be a coin
# toss over whose account it opens. Changing an address safely means re-proving the new
# one and resolving the collision - a whole feature, not a writable field. The pool's
# customer app client has `email` left out of its WriteAttributes, so this is enforced
# by Cognito as well as here.
LOCKED_PROFILE_FIELDS = {"email", "username", "password", "is_staff", "is_superuser",
                         "is_active", "id", "pk", "sub"}


class ProfileSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150, allow_blank=True, required=False)
    last_name = serializers.CharField(max_length=150, allow_blank=True, required=False)
    phone = serializers.CharField(max_length=32, required=False)

    def validate_phone(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError(
                "We need a phone number so we can reach you about a test drive."
            )
        return value


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_me(request.user))

    def patch(self, request):
        """Change your own name or phone number. Not your email - see above."""
        locked = LOCKED_PROFILE_FIELDS & set(request.data)
        if locked:
            return Response(
                {"detail": "Your email address is how you sign in, so it cannot be "
                           "changed here. Call us and we will change it for you."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = ProfileSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        fields = {k: v.strip() for k, v in serializer.validated_data.items()}

        user = request.user
        if fields:
            cognito.update_attributes(user.sub, **fields)

        refreshed = cognito.user_for(user.sub) or user
        if "phone" in fields or "first_name" in fields or "last_name" in fields:
            # The staff booking queue reads a snapshot rather than looking a customer up
            # per row, so it has to be re-stamped when the person edits themselves.
            from .store import bookings as booking_store

            booking_store.refresh_customer_snapshot(refreshed)
        return Response(_me(refreshed))


# --------------------------------------------------------------------------------------
# Password reset
# --------------------------------------------------------------------------------------


class PasswordResetView(APIView):
    """Ask for a reset link.

    Always 202, whatever the address, so it cannot be used to find out who has an
    account.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        language = "ja" if (request.data.get("language") or "").startswith("ja") else "en"

        user = cognito.user_for(email) if cognito.status_of(email) else None

        # Customers only. A staff account can edit inventory and other staff, so letting
        # a public endpoint mail a reset link to one would mean anyone who can read that
        # inbox could take it over. Staff who forget ask the owner.
        if user is not None and not user.is_staff and not user.is_superuser:
            raw = auth_store.start_reset(sub=user.sub, email=user.email,
                                         now=timezone.now())
            mail.send_password_reset_email(
                user, f"{seo.SITE_URL}/account/reset?token={raw}", language)

        return Response(
            {"detail": "If we have an account for that address, we have sent a link."},
            status=status.HTTP_202_ACCEPTED,
        )


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        raw = (request.data.get("token") or "").strip()
        password = request.data.get("password") or ""

        token = auth_store.reset_for_token(raw) if raw else None
        if token is None:
            return Response(
                {"detail": "That link has expired or already been used. "
                           "Please request a new one."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = cognito.user_for(token.sub)
        # Re-checked here as well as when issuing: a link minted before somebody became
        # staff must not still work afterwards.
        if user is None or user.is_staff or user.is_superuser:
            return Response(
                {"detail": "That link has expired or already been used. "
                           "Please request a new one."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if len(password) < MIN_PASSWORD_LENGTH:
            return Response(
                {"detail": f"This password is too short. It must contain at least "
                           f"{MIN_PASSWORD_LENGTH} characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            cognito.set_password(user.email, password)
        except cognito.CognitoError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)

        # Burns this link and every sibling. Deriving the token from the password hash
        # used to do that for free; the epoch counter buys it back.
        auth_store.complete_reset(token)

        tokens = cognito.authenticate(email=user.email, password=password)
        response = Response(_me(user))
        if tokens:
            authentication.set_session_cookies(
                response, tokens, secure=_secure_cookies(request))
        return response
