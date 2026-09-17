"""Staff page routes.

Mounted at `/api/staff/`. Under `/api/*` on purpose: that is the CloudFront behaviour
that disables caching and forwards cookies, both of which a signed-in page needs. The
`/api/cars/*` behaviour would strip the session cookie and refuse the POST, with no
Django log line to find.
"""

from django.urls import path

from . import views_questions

app_name = "staff"

urlpatterns = [
    path("questions/", views_questions.question_list, name="question-list"),
    path("questions/<str:question_id>/", views_questions.question_detail,
         name="question-detail"),
]
