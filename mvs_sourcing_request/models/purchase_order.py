# -*- coding: utf-8 -*-
"""purchase.order extension — sourcing links + one-way RFQ→grid back-sync (§4.1).

A candidate RFQ now carries one order line per candidate-vendor row (RFQs are
merged by vendor, so a single RFQ may cover several products). The vendor-row
link therefore lives on ``purchase.order.line`` — one PO line ↔ one vendor row —
and the back-sync runs per line.
"""

from odoo import _, fields, models

# Order-level keys whose change can affect a linked vendor row.
_SYNC_TRIGGER_KEYS = {"order_line", "payment_term_id", "date_order"}


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    sourcing_request_id = fields.Many2one(
        "sourcing.request", string="Sourcing Request", index=True,
        ondelete="set null", copy=False,
    )

    def write(self, vals):
        res = super().write(vals)
        # TD-002: one-way sync (RFQ -> vendor row), guarded against recursion.
        if self.env.context.get("skip_sourcing_sync"):
            return res
        if _SYNC_TRIGGER_KEYS & set(vals.keys()):
            sourced = self.filtered(
                lambda o: o.state in ("draft", "sent")
                and any(o.order_line.mapped("sourcing_vendor_line_id"))
            )
            if sourced:
                sourced._sync_to_sourcing_vendor_line()
        return res

    def _sync_to_sourcing_vendor_line(self):
        """Push the draft RFQ's negotiated figures back into the grid rows (BR-010).

        One-way only: vendor rows never write back to the RFQ. Each sourcing-linked
        PO line updates its own vendor row; the order-level payment term updates
        every linked row. ``delay`` is derived on date-only values to avoid
        timezone drift (TD-003).
        """
        for order in self:
            payment_term_id = order.payment_term_id.id or False
            for po_line in order.order_line:
                vendor = po_line.sourcing_vendor_line_id
                if not vendor:
                    continue
                sync_vals = {
                    "payment_term_id": payment_term_id,
                    "price": po_line.price_unit,
                }
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


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    sourcing_vendor_line_id = fields.Many2one(
        "sourcing.request.line.vendor", string="Sourcing Vendor Line", index=True,
        ondelete="set null", copy=False,
        help="The candidate-vendor row this RFQ line was generated from. Present "
             "only on candidate (Assisted) RFQ lines; Automatic-procurement lines "
             "leave it empty so they are never treated as candidate losers.",
    )
