from django.urls import include, path, re_path
from rest_framework.routers import DefaultRouter

from documents.api import views

app_name = "api"

router = DefaultRouter()
router.register("documents", views.DocumentViewSet, basename="document")
router.register("invoices", views.InvoiceViewSet, basename="invoice")

urlpatterns = [
    re_path(r"^invoices/export\.(?P<fmt>csv|json)$", views.export_invoices, name="invoice-export"),
    path("", include(router.urls)),
]
