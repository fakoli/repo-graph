from django.urls import path as register, re_path as register_regex, include
import django.urls.conf as url_functions
from .views import homepage as home, archive, wrapped, duplicate_view
from .missing_views import absent
from views import homepage as absolute_home

suffix = "current/"
urlpatterns = [
    register("café/", home, name="home"),
    register_regex(r"^archive/[0-9]+/$", archive, name="archive"),
    url_functions.path("direct/", home),
    register("again/", home),
    register("absolute/", absolute_home),
    register("callback/", lambda request: home(request)),
    register("computed/" + suffix, home),
    register("missing/", absent),
    register("decorated/", wrapped),
    register("duplicate/", duplicate_view),
    register("nested/", include("other_urls")),
    register(route="keyword/", view=home),
]
