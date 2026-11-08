from django.urls import path
from django.views.generic import TemplateView

from .views import home

app_name = "marketing"
urlpatterns = [
    path("", home, name="home"),
    path(
        "product/",
        TemplateView.as_view(template_name="marketing/product.html"),
        name="product",
    ),
    path(
        "roadmap/",
        TemplateView.as_view(template_name="marketing/roadmap.html"),
        name="roadmap",
    ),
    path(
        "trust/",
        TemplateView.as_view(template_name="marketing/trust.html"),
        name="trust",
    ),
    path(
        "hosting/",
        TemplateView.as_view(template_name="marketing/hosting.html"),
        name="hosting",
    ),
]
