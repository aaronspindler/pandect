from django.urls import path

from . import views

app_name = "marketing"
urlpatterns = [
    path("", views.page, name="home"),
    path("evidence/", views.page, name="evidence"),
    path("investigations/", views.page, name="investigations"),
    path("investigations/incidents/", views.page, name="investigation_incidents"),
    path("investigations/osint/", views.page, name="investigation_osint"),
    path(
        "investigations/communications/",
        views.page,
        name="investigation_communications",
    ),
    path(
        "investigations/vehicle-location/",
        views.page,
        name="investigation_vehicle_location",
    ),
    path("product/", views.product, name="product"),
    path("roadmap/", views.page, name="roadmap"),
    path("trust/", views.page, name="trust"),
    path("robots.txt", views.robots, name="robots"),
    path("sitemap.xml", views.sitemap, name="sitemap"),
]
