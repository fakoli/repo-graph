from django.urls.conf import path
from .views import homepage

path = lambda route, callback: callback
urlpatterns = [path("shadowed/", homepage)]
