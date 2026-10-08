def route(path):
    def decorator(method):
        return method
    return decorator


class Fake:
    @route("/lookalike")
    def endpoint(self):
        return "fake-route"


class FakeModel:
    _name = "sample.invoice"

    def action_post(self):
        return "fake-model"
