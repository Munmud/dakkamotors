from django.urls import path

from . import csrf_views, request_views
from .views import CarDetailView, CarListView

urlpatterns = [
    # Was a DefaultRouter ModelViewSet. There is no queryset to hang one on any more,
    # and the router's conventions were doing nothing two explicit routes do not.
    path("cars/", CarListView.as_view(), name="car-list"),
    path("cars/<str:slug>/", CarDetailView.as_view(), name="car-detail"),

    # The only write on the public site, and the only reason a token is needed. It
    # survived the removal of customer accounts because a cached page cannot carry one:
    # see csrf_views.py.
    path("auth/csrf/", csrf_views.CsrfView.as_view(), name="auth-csrf"),

    # Deliberately not under /api/cars/: that prefix is a CloudFront behaviour allowing
    # only GET, HEAD and OPTIONS, so a POST there is rejected by the CDN before Django
    # ever sees it - while working fine locally. The car, when there is one, travels in
    # the body.
    path("requests/", request_views.LeadCreateView.as_view(), name="request-create"),
]
