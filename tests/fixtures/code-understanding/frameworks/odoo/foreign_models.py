from odoo import models


class ForeignInvoice(models.Model):
    _name = "sample.invoice"

    def action_post(self):
        return "foreign"
