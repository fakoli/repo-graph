class Controller:
    pass


def route(paths=None, **options):
    def decorator(method):
        return method
    return decorator
