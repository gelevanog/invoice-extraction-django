from django.urls import path

from documents import views

app_name = "documents"

urlpatterns = [
    path("", views.queue, name="queue"),
    path("upload/", views.upload, name="upload"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/fields/<str:field>/", views.edit_field, name="edit_field"),
    path("<int:pk>/decide/", views.decide, name="decide"),
    path("<int:pk>/reprocess/", views.reprocess, name="reprocess"),
    path("<int:pk>/file/", views.document_file, name="file"),
]
