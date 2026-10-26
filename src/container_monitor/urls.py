from django.urls import path

from container_monitor import views

app_name = "container_monitor"
urlpatterns = [path("status/", views.status, name="status")]
