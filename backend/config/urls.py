"""
Root URL configuration.

Django now serves the site's HTML as well as its API. The React app is still what
visitors interact with, but the first response carries a real title, description,
canonical URL and structured data - so link previews, Bing and the answer engines see
something other than an empty <div>. See `cars/pages.py` for why it works this way.

The admin lives under /api/ because CloudFront routes that one prefix to the backend.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path

from cars import discovery, pages
from cars.models import Car
from cars.views import SignUploadView


def legacy_car_redirect(request, pk):
    """Permanently move /cars/34 to /cars/2008-daihatsu-tanto-x.

    301 rather than 302: it passes the ranking of anything already indexed at the old
    address on to the new one, and tells crawlers to stop asking for the numeric form.
    """
    car = Car.objects.filter(pk=pk).only("slug").first()
    if car is None:
        return redirect("/", permanent=False)
    return redirect(car.get_absolute_url(), permanent=True)


urlpatterns = [
    # Declared before the admin so it is matched first; it is admin-only functionality
    # and belongs under the same prefix, but is a DRF view rather than an admin page.
    path("api/admin/uploads/sign/", SignUploadView.as_view(), name="sign-upload"),
    path("api/admin/", admin.site.urls),
    path("api/", include("cars.urls")),
    # Crawler-facing files. Previously 403s, because the private S3 bucket answered
    # AccessDenied for objects that were never uploaded.
    path("robots.txt", discovery.robots_txt, name="robots-txt"),
    path("sitemap.xml", discovery.sitemap_xml, name="sitemap-xml"),
    path("llms.txt", discovery.llms_txt, name="llms-txt"),
    # Rendered pages.
    path("", pages.home, name="home"),
    path("cars/<int:pk>", legacy_car_redirect),
    path("cars/<int:pk>/", legacy_car_redirect),
    path("cars/<slug:slug>", pages.car_detail, name="car-page"),
    path("cars/<slug:slug>/", pages.car_detail),
    # App-only routes. They need a shell or the URL 404s, but they carry noindex.
    path("cars/<slug:slug>/test-drive", pages.book_test_drive_page),
    path("account", pages.account_page),
    path("account/", pages.account_page),
    path("account/<path:rest>", pages.account_page),
]

# In production, uploads live in S3 and are served by CloudFront at /media/*, so Django
# never sees these URLs. Locally there is no CDN, so Django serves the files itself.
if settings.DEBUG and not settings.AWS_STORAGE_BUCKET_NAME:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

admin.site.site_header = "Dakka Motors"
admin.site.site_title = "Dakka Motors"
admin.site.index_title = "Inventory administration"
