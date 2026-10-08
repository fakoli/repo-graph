from odoo import api, http, models


def extra_wrapper(method):
    return method


class StackedRoute(http.Controller):
    @extra_wrapper
    @http.route("/stacked/café", type="http")
    def endpoint(self):
        return "stacked"


class UnsupportedDecorator(models.Model):
    _name = "sample.decorated"

    @api.depends("total")
    def action_post(self):
        return "unsupported-decorator"
