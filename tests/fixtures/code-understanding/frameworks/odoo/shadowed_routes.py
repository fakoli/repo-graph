from odoo import http

http = object()


class Shadowed:
    @http.route("/shadowed")
    def endpoint(self):
        return "shadowed"
