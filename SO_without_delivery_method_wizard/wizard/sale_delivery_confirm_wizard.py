from odoo import fields, models


class SaleDeliveryConfirmWizard(models.TransientModel):
    """Wizard shown when confirming a Sales Order that has no delivery method."""

    _name = "sale.delivery.confirm.wizard"
    _description = "Sales Order Delivery Method Confirmation Wizard"

    sale_order_id = fields.Many2one(
        comodel_name="sale.order",
        string="Sales Order",
        required=True,
        ondelete="cascade",
    )

    # ---------------------------------------------------------------------------
    # Button Actions
    # ---------------------------------------------------------------------------

    def action_add_delivery(self):
        """Close this wizard and open the delivery method wizard on the current Sales Order."""
        self.ensure_one()
        return self.sale_order_id.action_open_delivery_wizard()

    def action_confirm_without_delivery(self):
        """Confirm the Sales Order, bypassing the delivery method check."""
        self.ensure_one()
        self.sale_order_id.with_context(skip_delivery_check=True).action_confirm()
        return {"type": "ir.actions.act_window_close"}

    def action_cancel(self):
        """Close the wizard without any further action."""
        return {"type": "ir.actions.act_window_close"}
