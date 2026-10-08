from .views import homepage


def path(route, callback):
    return callback


urlpatterns = [path("lookalike/", homepage)]
