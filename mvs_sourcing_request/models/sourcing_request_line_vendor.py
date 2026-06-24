# -*- coding: utf-8 -*-
"""Candidate vendor row — comparison + decision row for a sourcing line (TDD §3.1)."""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import float_compare

# Figure / decision fields whose change is logged to the request chatter (BR-011).
TRACKED_FIELDS = {
    "price", "delay", "min_qty", "payment_term_id",
    "qty_to_source", "selected", "selection_reason",
}


class SourcingRequestLineVendor(models.Model):
    _name = "sourcing.request.line.vendor"
    _description = "Sourcing Request Candidate Vendor"
    _rec_name = "partner_id"
    _order = "price, delay"

    line_id = fields.Many2one(
        "sourcing.request.line", string="Request Line", required=True, index=True,
        ondelete="cascade",
    )
    partner_id = fields.Many2one(
        "res.partner", string="Vendor", required=True, index=True, ondelete="restrict",
    )
    price = fields.Float(
        string="Price", digits="Product Price",
        help="Editable — overridden with RFQ replies; master data is not updated.",
    )
    delay = fields.Integer(
        string="Lead Time (days)", help="Editable; drives the RFQ date_planned.",
    )
    min_qty = fields.Float(string="Min Qty", digits="Product Unit of Measure")
    currency_id = fields.Many2one(
        "res.currency", string="Currency", ondelete="restrict",
    )
    payment_term_id = fields.Many2one(
        "account.payment.term", string="Payment Terms", ondelete="set null",
    )
    rfq_id = fields.Many2one(
        "purchase.order", string="RFQ", index=True, ondelete="set null", copy=False,
        help="The linked draft RFQ/PO for this vendor.",
    )
    qty_to_source = fields.Float(
        string="Qty to Source", default=0.0, digits="Product Unit of Measure",
    )
    selected = fields.Boolean(string="Selected", default=False)
    selection_reason = fields.Char(string="Selection Reason")
    is_best_price = fields.Boolean(string="Best Price", compute="_compute_best")
    is_best_delay = fields.Boolean(string="Best Lead Time", compute="_compute_best")

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------
    @api.depends(
        "price", "delay",
        "line_id.vendor_line_ids.price", "line_id.vendor_line_ids.delay",
    )
    def _compute_best(self):
        for vendor in self:
            siblings = vendor.line_id.vendor_line_ids
            prices = [v.price for v in siblings]
            delays = [v.delay for v in siblings]
            vendor.is_best_price = bool(prices) and vendor.price == min(prices)
            vendor.is_best_delay = bool(delays) and vendor.delay == min(delays)

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains("qty_to_source")
    def _check_qty_to_source(self):
        """BR-005 — qty_to_source must lie within 0..required."""
        for vendor in self:
            required = vendor.line_id.product_qty
            rounding = vendor.line_id.product_id.uom_id.rounding or 0.01
            if vendor.qty_to_source < 0 or float_compare(
                vendor.qty_to_source, required, precision_rounding=rounding
            ) > 0:
                raise ValidationError(
                    _(
                        "Qty to Source (%(qty)s) for vendor %(vendor)s must be "
                        "between 0 and the required quantity (%(req)s).",
                        qty=vendor.qty_to_source,
                        vendor=vendor.partner_id.display_name,
                        req=required,
                    )
                )

    # ------------------------------------------------------------------
    # CRUD — chatter logging (BR-011 / D-012)
    # ------------------------------------------------------------------
    def write(self, vals):
        tracked = TRACKED_FIELDS & set(vals.keys())
        snapshots = {}
        if tracked:
            for vendor in self:
                snapshots[vendor.id] = {f: vendor[f] for f in tracked}
        res = super().write(vals)
        if tracked:
            self._log_grid_changes(snapshots, tracked)
        return res

    def _log_grid_changes(self, snapshots, tracked):
        for vendor in self:
            request = vendor.line_id.request_id
            if not request:
                continue
            changes = []
            for field_name in tracked:
                old = snapshots.get(vendor.id, {}).get(field_name)
                new = vendor[field_name]
                if old == new:
                    continue
                label = self._fields[field_name].string
                changes.append(
                    _("%(label)s: %(old)s → %(new)s",
                      label=label,
                      old=self._format_value(old),
                      new=self._format_value(new))
                )
            if changes:
                request.message_post(
                    body=_(
                        "Vendor %(vendor)s on %(product)s — %(changes)s",
                        vendor=vendor.partner_id.display_name,
                        product=vendor.line_id.product_id.display_name,
                        changes="; ".join(changes),
                    )
                )

    @staticmethod
    def _format_value(value):
        if hasattr(value, "display_name"):
            return value.display_name or _("(none)")
        return value

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _apply_to_rfq(self):
        """Push the final grid figures onto the linked draft RFQ (TD-001)."""
        for vendor in self:
            order = vendor.rfq_id
            if not order or order.state not in ("draft", "sent"):
                continue
            po_line = order.order_line.filtered(
                lambda l: l.product_id == vendor.line_id.product_id
            )[:1]
            order_vals = {"payment_term_id": vendor.payment_term_id.id or False}
            if po_line:
                order_vals["order_line"] = [(1, po_line.id, {
                    "price_unit": vendor.price,
                    "product_qty": vendor.qty_to_source or vendor.line_id.product_qty,
                })]
            order.with_context(skip_sourcing_sync=True).write(order_vals)
