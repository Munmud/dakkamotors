from django.urls import path
from rest_framework.routers import DefaultRouter

from . import auth_views, booking_views, notification_views, qa_views
from .views import CarViewSet

router = DefaultRouter()
router.register(r"cars", CarViewSet, basename="car")

urlpatterns = router.urls + [
    # Customer accounts. Everything under /api/ has CDN caching disabled and cookies
    # forwarded, which is what these need and what the cached pages cannot offer.
    path("auth/csrf/", auth_views.CsrfView.as_view(), name="auth-csrf"),
    path("auth/register/", auth_views.RegisterView.as_view(), name="auth-register"),
    path("auth/login/", auth_views.LoginView.as_view(), name="auth-login"),
    path("auth/logout/", auth_views.LogoutView.as_view(), name="auth-logout"),
    path("auth/me/", auth_views.MeView.as_view(), name="auth-me"),
    path("auth/verify/", auth_views.VerifyView.as_view(), name="auth-verify"),
    path("auth/resend/", auth_views.ResendVerificationView.as_view(), name="auth-resend"),
    path("auth/password-reset/", auth_views.PasswordResetView.as_view(),
         name="auth-password-reset"),
    path("auth/password-reset/confirm/", auth_views.PasswordResetConfirmView.as_view(),
         name="auth-password-reset-confirm"),

    # Deliberately not under /api/cars/: that prefix is a CloudFront behaviour allowing
    # only GET, HEAD and OPTIONS, so a POST there is rejected by the CDN before Django
    # ever sees it - while working fine locally.
    path("questions/", qa_views.QuestionListCreateView.as_view(), name="question-list"),

    path("notifications/", notification_views.NotificationListView.as_view(),
         name="notification-list"),
    path("notifications/read/", notification_views.NotificationReadView.as_view(),
         name="notification-read"),

    path("test-drive/slots/", booking_views.SlotListView.as_view(), name="slot-list"),
    path(
        "test-drive/bookings/",
        booking_views.BookingListCreateView.as_view(),
        name="booking-list",
    ),
    path(
        "test-drive/bookings/<int:pk>/cancel/",
        booking_views.BookingCancelView.as_view(),
        name="booking-cancel",
    ),
    path(
        "test-drive/bookings/<int:pk>/reschedule/",
        booking_views.BookingRescheduleView.as_view(),
        name="booking-reschedule",
    ),
]
