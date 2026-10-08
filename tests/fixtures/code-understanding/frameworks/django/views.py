# UTF-8 range control: café, 雪.
def homepage(request):
    return "home"


def archive(request):
    return "archive"


def decorate(callback):
    return callback


@decorate
def wrapped(request):
    return "wrapped"


def duplicate_view(request):
    return "first"


def duplicate_view(request):
    return "second"
