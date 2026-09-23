"""
Root URL configuration.

Django now serves the site's HTML as well as its API. The React app is still what
visitors interact with, but the first response carries a real title, description,
canonical URL and structured data - so link previews, Bing and the answer engines see
something other than an empty <div>. See `cars/pages.py` for why it works this way.

The staff pages live under /api/ because CloudFront routes that one prefix to the
backend with cookies forwarded and caching off.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect
from django.urls import include, path

from cars import discovery, pages
from cars.store import cars as car_store
from cars.views import SignUploadView


def legacy_car_redirect(request, pk):
    """Permanently move /cars/34 to /cars/2008-daihatsu-tanto-x.

    301 rather than 302: it passes the ranking of anything already indexed at the old
    address on to the new one, and tells crawlers to stop asking for the numeric form.
    """
    car = car_store.find(str(pk))
    if car is None:
        # Ids were integers before the move to DynamoDB, so a link shared back then may
        # point at one that no longer resolves directly. The pointer item is written by
        # the migration importer for exactly this.
        slug = car_store.slug_for_legacy_id(pk)
        if slug:
            return redirect(f"/cars/{slug}", permanent=True)
        return redirect("/", permanent=False)
    return redirect(car.get_absolute_url(), permanent=True)


urlpatterns = [
    # A DRF view rather than a staff page, so it is declared here rather than in
    # cars/staff/urls.py -- but it moved off /api/admin/ with everything else, so that
    # prefix now 404s outright instead of half-working from a stale bookmark.
    path("api/staff/uploads/sign/", SignUploadView.as_view(), name="sign-upload"),
    # Server-rendered staff pages, replacing the Django admin. Under /api/ because that
    # is the one prefix CloudFront routes to the backend with cookies forwarded and
    # caching off.
    path("api/staff/", include("cars.staff.urls")),
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
    path("request-a-car", pages.request_car_page, name="request-car"),
    path("request-a-car/", pages.request_car_page),
    # Indexable, unlike the account routes below: Meta looks for this page when it
    # reviews an advertisement, and it is linked from the footer of every page.
    path("privacy", pages.privacy_page, name="privacy"),
    path("privacy/", pages.privacy_page),
    path("account", pages.account_page),
    path("account/", pages.account_page),
    path("account/<path:rest>", pages.account_page),
]

# In production, uploads live in S3 and are served by CloudFront at /media/*, so Django
# never sees these URLs. Locally there is no CDN, so Django serves the files itself.
if settings.DEBUG and not settings.AWS_STORAGE_BUCKET_NAME:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

