"""Customer accounts.

Separate from staff entirely: these users are never `is_staff`, so Django's admin turns
them away on its own. Email doubles as the username, which keeps Django's own auth and
password handling unchanged - no custom user model, no risky AUTH_USER_MODEL swap on a
database that already has accounts.

**No `User` row exists until the address has been proved.** A sign-up writes a
`PendingRegistration` and sends a link; clicking it is what creates the account. That is
why nothing downstream asks whether a customer is verified - an unverified person simply
has no account.
"""

import datetime as dt
import hashlib
import logging
import secrets

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import MinimumLengthValidator
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from . import mail, seo
from .booking_models import CustomerProfile, PendingRegistration

logger = logging.getLogger(__name__)
User = get_user_model()

PENDING_DAYS = 3

#: Customers are held to length alone. Staff keep the full AUTH_PASSWORD_VALIDATORS set,
#: because that is what the admin applies - and a staff account can edit inventory and
#: other staff, where a customer account holds only their own bookings.
CUSTOMER_PASSWORD_VALIDATORS = [MinimumLengthValidator(min_length=6)]


class AuthThrottle(AnonRateThrottle):
    """Blunts credential stuffing, bulk sign-ups and inbox bombing.

    Matters more than usual: customers may choose weak passwords, so the rate limit is
    doing work that password complexity is not.
    """

    scope = "auth"


def _me(user):
    profile = getattr(user, "customer_profile", None)
    return {
        "name": user.get_full_name() or user.username,
        "email": user.email,
        "phone": profile.phone if profile else "",
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


def _hash_token(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CsrfView(APIView):
    """Hands the app a CSRF token.

    It cannot come from the rendered page: those are cached at the CDN for five minutes
    with cookies ignored, so a token set there would be handed to every visitor alike -
    which is precisely the thing CSRF protection is supposed to prevent. This endpoint
    lives under /api/, where caching is disabled.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"csrfToken": get_token(request)})


# --------------------------------------------------------------------------------------
# Registration - two steps, because the account does not exist until step two
# --------------------------------------------------------------------------------------


class RegisterSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=32)
    password = serializers.CharField(write_only=True)
    next = serializers.CharField(required=False, allow_blank=True)
    language = serializers.CharField(required=False, allow_blank=True)

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(username=value).exists() or User.objects.filter(
            email__iexact=value
        ).exists():
            # This does reveal that an account exists. Kept deliberately: a customer who
            # has forgotten needs telling, and a used-car customer list is not a secret.
            raise serializers.ValidationError(
                "An account with that email already exists. Try signing in instead."
            )
        return value

    def validate_password(self, value):
        try:
            validate_password(value, password_validators=CUSTOMER_PASSWORD_VALIDATORS)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value


class RegisterView(APIView):
    """Step one: remember what they typed and email them a link.

    Returns 202 and **no session** - there is nothing to log in to yet.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Swept here rather than on a timer: a scheduled job would wake Aurora and undo
        # the scale-to-zero saving, and this is the one moment the table is being
        # written anyway.
        PendingRegistration.objects.filter(expires_at__lt=timezone.now()).delete()

        raw_token = secrets.token_urlsafe(32)
        language = "ja" if (data.get("language") or "").startswith("ja") else "en"

        # update_or_create, not create: a typo on the first attempt must not lock that
        # address out for three days. Re-submitting is also how "resend" works.
        pending, _ = PendingRegistration.objects.update_or_create(
            email=data["email"],
            defaults={
                "name": data["name"].strip(),
                "phone": data["phone"].strip(),
                "password_hash": make_password(data["password"]),
                "token_hash": _hash_token(raw_token),
                "next_path": safe_next(data.get("next")),
                "language": language,
                "expires_at": timezone.now() + dt.timedelta(days=PENDING_DAYS),
            },
        )

        mail.send_verification_email(
            pending, f"{seo.SITE_URL}/account/verify?token={raw_token}"
        )
        return Response(
            {"detail": "Check your email to finish creating your account.",
             "email": pending.email},
            status=status.HTTP_202_ACCEPTED,
        )


class VerifyView(APIView):
    """Step two: the link. This is what actually creates the account."""

    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        raw_token = (request.data.get("token") or "").strip()
        if not raw_token:
            return Response({"detail": "That link is not valid."},
                            status=status.HTTP_400_BAD_REQUEST)

        pending = PendingRegistration.objects.filter(
            token_hash=_hash_token(raw_token)
        ).first()

        if pending is None:
            return Response(
                {"detail": "That link has already been used, or is not valid. "
                           "Try signing in, or register again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if pending.has_expired:
            pending.delete()
            return Response(
                {"detail": "That link has expired. Please register again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        first_name, _, last_name = pending.name.partition(" ")
        try:
            with transaction.atomic():
                user = User(
                    username=pending.email,
                    email=pending.email,
                    first_name=first_name[:150],
                    last_name=last_name[:150],
                    is_staff=False,
                    is_superuser=False,
                )
                # Already hashed at sign-up; carried across without ever having been
                # stored in plaintext.
                user.password = pending.password_hash
                user.save()
                CustomerProfile.objects.create(user=user, phone=pending.phone)
                next_path = pending.next_path
                pending.delete()
        except IntegrityError:
            pending.delete()
            return Response(
                {"detail": "An account with that email already exists. Please sign in."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        login(request, user)
        return Response({**_me(user), "next": next_path or "/account"})


class ResendVerificationView(APIView):
    """Send the link again.

    Answers identically whether or not a pending sign-up exists, so it cannot be used to
    discover addresses or to bomb someone's inbox.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        pending = PendingRegistration.objects.filter(email=email).first()

        if pending is not None and not pending.has_expired:
            raw_token = secrets.token_urlsafe(32)
            pending.token_hash = _hash_token(raw_token)
            pending.expires_at = timezone.now() + dt.timedelta(days=PENDING_DAYS)
            pending.save(update_fields=["token_hash", "expires_at"])
            mail.send_verification_email(
                pending, f"{seo.SITE_URL}/account/verify?token={raw_token}"
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

        user = authenticate(request, username=email, password=password)
        if user is None or not user.is_active:
            # One message for both cases, so the response cannot be used to discover
            # which addresses have accounts.
            return Response(
                {"detail": "Email or password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        login(request, user)
        return Response(_me(user))


class LogoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_me(request.user))


# --------------------------------------------------------------------------------------
# Password reset
# --------------------------------------------------------------------------------------

reset_token = PasswordResetTokenGenerator()


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

        # Customers only. A staff account can edit inventory and other staff, so letting
        # a public endpoint mail a reset link to one would mean anyone who can read that
        # inbox could take it over. Staff who forget ask the owner, who resets it in the
        # admin.
        user = User.objects.filter(
            email__iexact=email, is_active=True, is_staff=False, is_superuser=False
        ).first()

        if user is not None:
            token = reset_token.make_token(user)
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            mail.send_password_reset_email(
                user, f"{seo.SITE_URL}/account/reset?uid={uid}&token={token}", language
            )

        return Response(
            {"detail": "If we have an account for that address, we have sent a link."},
            status=status.HTTP_202_ACCEPTED,
        )


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        uid = request.data.get("uid") or ""
        token = request.data.get("token") or ""
        password = request.data.get("password") or ""

        try:
            user = User.objects.get(pk=force_str(urlsafe_base64_decode(uid)))
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            user = None

        # Re-checked here as well as when issuing: a link minted before someone became
        # staff must not still work afterwards.
        if user is None or user.is_staff or user.is_superuser or not reset_token.check_token(user, token):
            return Response(
                {"detail": "That link has expired or already been used. "
                           "Please request a new one."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_password(password, user, password_validators=CUSTOMER_PASSWORD_VALIDATORS)
        except DjangoValidationError as exc:
            return Response({"detail": " ".join(exc.messages)},
                            status=status.HTTP_400_BAD_REQUEST)

        user.set_password(password)
        user.save(update_fields=["password"])
        # Changing the password is what invalidates the link: the token hash is derived
        # from it, so every outstanding reset link for this account dies here.
        login(request, user)
        return Response(_me(user))
