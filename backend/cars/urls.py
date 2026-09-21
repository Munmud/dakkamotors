from django.urls import path

from . import auth_views, booking_views, notification_views, qa_views, request_views
from .views import CarDetailView, CarListView

urlpatterns = [
    # Was a DefaultRouter ModelViewSet. There is no queryset to hang one on any more,
    # and the router's conventions were doing nothing two explicit routes do not.
    path("cars/", CarListView.as_view(), name="car-list"),
    path("cars/<str:slug>/", CarDetailView.as_view(), name="car-detail"),

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
    path("requests/", request_views.CarRequestCreateView.as_view(),
         name="request-create"),

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
    # <str:pk>, not <int:pk>: a booking id is a sortable string now, not a sequence.
    # Leaving it as int would 404 before the view ran, which reads as "no such URL"
    # rather than as the "booking not found" the customer should be told.
    path(
        "test-drive/bookings/<str:pk>/cancel/",
        booking_views.BookingCancelView.as_view(),
        name="booking-cancel",
    ),
    path(
        "test-drive/bookings/<str:pk>/reschedule/",
        booking_views.BookingRescheduleView.as_view(),
        name="booking-reschedule",
    ),
]
