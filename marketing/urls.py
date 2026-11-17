from django.urls import path
from django.views.generic import TemplateView

from .views import evidence, home, product

app_name = "marketing"
urlpatterns = [
    path("", home, name="home"),
    path("evidence/", evidence, name="evidence"),
    path("product/", product, name="product"),
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
]
