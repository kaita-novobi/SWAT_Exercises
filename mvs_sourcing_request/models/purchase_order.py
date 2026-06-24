# -*- coding: utf-8 -*-
"""purchase.order extension — sourcing links + one-way RFQ→grid back-sync (§4.1)."""

from odoo import _, fields, models

# Order-level keys whose change can affect the linked vendor row.
_SYNC_TRIGGER_KEYS = {"order_line", "payment_term_id", "date_order"}


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    sourcing_request_id = fields.Many2one(
        "sourcing.request", string="Sourcing Request", index=True,
        ondelete="set null", copy=False,
    )
    sourcing_vendor_line_id = fields.Many2one(
        "sourcing.request.line.vendor", string="Sourcing Vendor Line", index=True,
        ondelete="set null", copy=False,
    )

    def write(self, vals):
        res = super().write(vals)
        # TD-002: one-way sync (RFQ -> vendor row), guarded against recursion.
        if self.env.context.get("skip_sourcing_sync"):
            return res
        if _SYNC_TRIGGER_KEYS & set(vals.keys()):
            sourced = self.filtered(
                lambda o: o.sourcing_vendor_line_id and o.state in ("draft", "sent")
            )
            if sourced:
                sourced._sync_to_sourcing_vendor_line()
        return res

    def _sync_to_sourcing_vendor_line(self):
        """Push the draft RFQ's negotiated figures back into the grid row (BR-010).

        One-way only: the vendor row never writes back to the RFQ.
        ``delay`` is derived on date-only values to avoid timezone drift (TD-003).
        """
        for order in self:
            vendor = order.sourcing_vendor_line_id
            po_line = order.order_line.filtered(
                lambda l: l.product_id == vendor.line_id.product_id
            )[:1]
            sync_vals = {"payment_term_id": order.payment_term_id.id or False}
            if po_line:
                sync_vals["price"] = po_line.price_unit
                if po_line.date_planned and order.date_order:
                    sync_vals["delay"] = (
                        po_line.date_planned.date() - order.date_order.date()
                    ).days
            vendor.write(sync_vals)

    def action_view_sourcing_request(self):
        """R08 — navigate from the PO back to its sourcing request."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Sourcing Request"),
            "res_model": "sourcing.request",
            "view_mode": "form",
            "res_id": self.sourcing_request_id.id,
        }
