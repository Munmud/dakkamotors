"""Customer accounts.

Separate from staff entirely: these users are never `is_staff`, so Django's admin turns
them away on its own. Email doubles as the username, which keeps Django's own auth and
password handling unchanged - no custom user model, no risky AUTH_USER_MODEL swap on a
database that already has accounts.

Email verification is deliberately not here yet (see docs/TODO.md). Because an address
is therefore unproven, registration and login are throttled and bookings are capped.
"""

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.middleware.csrf import get_token
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .booking_models import CustomerProfile

User = get_user_model()


class AuthThrottle(AnonRateThrottle):
    """Blunts credential stuffing and bulk sign-ups.

    Matters more than usual here: with no email verification, an account costs nothing
    to create.
    """

    scope = "auth"


def _me(user):
    profile = getattr(user, "customer_profile", None)
    return {
        "name": user.get_full_name() or user.username,
        "email": user.email,
        "phone": profile.phone if profile else "",
    }


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


class RegisterSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=32)
    password = serializers.CharField(write_only=True)

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(username=value).exists() or User.objects.filter(
            email__iexact=value
        ).exists():
            raise serializers.ValidationError(
                "An account with that email already exists. Try signing in instead."
            )
        return value

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value


class RegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        first_name, _, last_name = data["name"].strip().partition(" ")

        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=data["email"],
                    email=data["email"],
                    password=data["password"],
                    first_name=first_name[:150],
                    last_name=last_name[:150],
                )
                # Customers are not staff. Django's admin refuses them on that alone,
                # so there is no second place this has to be enforced.
                user.is_staff = False
                user.is_superuser = False
                user.save(update_fields=["is_staff", "is_superuser"])
                CustomerProfile.objects.create(user=user, phone=data["phone"].strip())
        except IntegrityError:
            # Two sign-ups with the same address at once.
            return Response(
                {"detail": "An account with that email already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        login(request, user)
        return Response(_me(user), status=status.HTTP_201_CREATED)


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
