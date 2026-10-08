from odoo import http

prefix = "/orders"


class Orders(http.Controller):
    @http.route(["/orders/café", "/orders/<int:order_id>"], type="http")
    def orders(self, order_id=None):
        return "orders"

    @http.route()
    def inherited(self):
        return "inherited"

    @http.route(prefix + "/computed")
    def computed(self):
        return "computed"
