"""Staff page routes.

Mounted at `/api/staff/`. Under `/api/*` on purpose: that is the CloudFront behaviour
that disables caching and forwards cookies, both of which a signed-in page needs. The
`/api/cars/*` behaviour would strip the session cookie and refuse the POST, with no
Django log line to find.
"""

from django.urls import path

from . import views_bookings
from . import views_cars
from . import views_questions

app_name = "staff"

urlpatterns = [
    path("questions/", views_questions.question_list, name="question-list"),
    path("questions/<str:question_id>/", views_questions.question_detail,
         name="question-detail"),
    path("bookings/", views_bookings.booking_list, name="booking-list"),
    path("bookings/<str:booking_id>/", views_bookings.booking_detail,
         name="booking-detail"),
    path("slots/", views_bookings.slot_list, name="slot-list"),
    path("slots/<str:slot_id>/toggle/", views_bookings.slot_toggle,
         name="slot-toggle"),
    path("cars/", views_cars.car_list, name="car-list"),
    path("cars/add/", views_cars.car_add, name="car-add"),
    path("cars/<str:car_id>/", views_cars.car_edit, name="car-edit"),
]
