from odoo import models


class SaleOrder(models.Model):
    """Extends sale.order to intercept confirmation when no delivery method is set."""

    _inherit = "sale.order"

    def action_confirm(self):
        """Override to show a delivery method wizard when carrier_id is not set.

        If the order already has a delivery method (carrier_id) or the context
        flag `skip_delivery_check` is set, fall through to the standard
        confirmation flow. Otherwise, open the delivery method popup wizard so
        the user can decide how to proceed.
        """
        self.ensure_one()
        if self.carrier_id or self.env.context.get("skip_delivery_check"):
            return super().action_confirm()

        wizard = self.env["sale.delivery.confirm.wizard"].create(
            {"sale_order_id": self.id}
        )
        return {
            "type": "ir.actions.act_window",
            "name": "Delivery Method",
            "res_model": "sale.delivery.confirm.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }
