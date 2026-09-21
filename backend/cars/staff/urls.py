"""Staff page routes.

Mounted at `/api/staff/`. Under `/api/*` on purpose: that is the CloudFront behaviour
that disables caching and forwards cookies, both of which a signed-in page needs. The
`/api/cars/*` behaviour would strip the session cookie and refuse the POST, with no
Django log line to find.
"""

from django.urls import path

from . import views_auth
from . import views_bookings
from . import views_cars
from . import views_customers
from . import views_questions
from . import views_requests
from . import views_schedules
from . import views_staff

app_name = "staff"

urlpatterns = [
    # The bare prefix. It is `sign_in_url`'s default `next` and the callback's
    # fallback destination, so without it a successful sign-in ended on a 404.
    path("", views_auth.index, name="index"),
    path("auth/sign-in/", views_auth.sign_in, name="sign-in"),
    # No trailing slash: this is what Cognito is configured to call, and letting
    # APPEND_SLASH 301 it added a hop that carried the authorization code through
    # a redirect for no reason. Registered on the app client as the same string.
    path("auth/callback", views_auth.callback, name="auth-callback"),
    path("auth/sign-out/", views_auth.sign_out, name="sign-out"),
    path("signed-out", views_auth.signed_out, name="signed-out"),
    path("auth/not-configured", views_auth.not_configured,
         name="not-configured"),
    path("questions/", views_questions.question_list, name="question-list"),
    path("questions/<str:question_id>/", views_questions.question_detail,
         name="question-detail"),
    path("requests/", views_requests.request_list, name="request-list"),
    path("requests/<str:request_id>/", views_requests.request_detail,
         name="request-detail"),
    path("bookings/", views_bookings.booking_list, name="booking-list"),
    path("bookings/<str:booking_id>/", views_bookings.booking_detail,
         name="booking-detail"),
    path("slots/", views_bookings.slot_list, name="slot-list"),
    path("slots/<str:slot_id>/toggle/", views_bookings.slot_toggle,
         name="slot-toggle"),
    path("cars/", views_cars.car_list, name="car-list"),
    path("cars/add/", views_cars.car_add, name="car-add"),
    path("cars/<str:car_id>/", views_cars.car_edit, name="car-edit"),
    path("availability/", views_schedules.schedule_list, name="schedule-list"),
    path("availability/add/", views_schedules.schedule_add, name="schedule-add"),
    path("availability/<str:schedule_id>/", views_schedules.schedule_edit,
         name="schedule-edit"),
    path("customers/", views_customers.customer_list, name="customer-list"),
    path("accounts/", views_staff.staff_list, name="staff-list"),
    path("accounts/add/", views_staff.staff_add, name="staff-add"),
    path("accounts/<str:username>/", views_staff.staff_edit, name="staff-edit"),
]
