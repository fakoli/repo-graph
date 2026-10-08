# UTF-8 evidence: café, 雪.
from odoo import api, models


class Order(models.Model):
    _name = "sample.order"

    def action_confirm(self):
        return self.env["sample.invoice"].action_post()


class Picking(models.Model):
    _name = "sample.picking"

    def button_validate(self):
        return "validated"


class Invoice(models.Model):
    _name = "sample.invoice"

    def action_post(self):
        return "posted"


class Scheduler(models.Model):
    _name = "sample.rule"

    @api.model
    def run_scheduler(self):
        return "scheduled"


class OrderExtension(models.Model):
    _inherit = "sample.order"

    def _action_confirm(self):
        return super()._action_confirm()


suffix = "computed"


class ComputedName(models.Model):
    _name = "sample." + suffix

    def action_post(self):
        return "computed-model"
