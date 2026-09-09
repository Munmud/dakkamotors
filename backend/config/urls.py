"""
Root URL configuration.

Everything is mounted under /api/ — including the admin — because CloudFront routes the
single path pattern /api/* to API Gateway and serves everything else from the React
bundle in S3. Keeping the admin inside that prefix means one origin behavior covers the
whole Django application.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("api/admin/", admin.site.urls),
    path("api/", include("cars.urls")),
]

# In production, uploads live in S3 and are served by CloudFront at /media/*, so Django
# never sees these URLs. Locally there is no CDN, so Django serves the files itself.
if settings.DEBUG and not settings.AWS_STORAGE_BUCKET_NAME:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

admin.site.site_header = "Dakka Motors"
admin.site.site_title = "Dakka Motors"
admin.site.index_title = "Inventory administration"
