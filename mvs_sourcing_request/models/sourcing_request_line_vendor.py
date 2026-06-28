# -*- coding: utf-8 -*-
"""Candidate vendor row — comparison + decision row for a sourcing line (TDD §3.1)."""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import float_compare, formatLang

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
    # MVS-025 — immutable baseline snapshot (stored), captured once in create().
    base_price = fields.Float(
        string="Baseline Price", digits="Product Price", readonly=True,
        help="Snapshot of Price at row creation; never updated afterwards.",
    )
    base_delay = fields.Integer(string="Baseline Lead Time (days)", readonly=True)
    base_min_qty = fields.Float(
        string="Baseline Min Qty", digits="Product Unit of Measure", readonly=True,
    )
    base_payment_term_id = fields.Many2one(
        "account.payment.term", string="Baseline Payment Terms",
        ondelete="set null", readonly=True,
    )
    # MVS-025 — change indicators (computed, store=False).
    has_change = fields.Boolean(string="Changed", compute="_compute_change_flags")
    price_changed = fields.Boolean(string="Price Changed", compute="_compute_change_flags")
    delay_changed = fields.Boolean(string="Lead Time Changed", compute="_compute_change_flags")
    min_qty_changed = fields.Boolean(string="Min Qty Changed", compute="_compute_change_flags")
    payment_term_changed = fields.Boolean(
        string="Payment Terms Changed", compute="_compute_change_flags",
    )
    # MVS-024 A1 — total line cost read-out (computed, store=False).
    total_line_cost_display = fields.Char(
        string="Total Line Cost", compute="_compute_total_line_cost_display",
        help="Unit price × Qty to Source; before allocation, × the required "
             "quantity, marked '(est.)'. Single currency per line assumed (D-006).",
    )
    is_best_total = fields.Boolean(
        string="Best Total", compute="_compute_is_best_total",
        help="Lowest Total Line Cost among the line's candidate vendors (R02).",
    )

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

    def _get_total_amount(self):
        """R01/D-001 — price × Qty to Source, or × required qty as an estimate."""
        self.ensure_one()
        qty = self.qty_to_source if self.qty_to_source > 0 else self.line_id.product_qty
        return self.price * qty

    @api.depends("price", "qty_to_source", "line_id.product_qty", "currency_id")
    def _compute_total_line_cost_display(self):
        for vendor in self:
            currency = (
                vendor.currency_id
                or vendor.line_id.request_id.company_id.currency_id
            )
            label = formatLang(self.env, vendor._get_total_amount(), currency_obj=currency)
            if vendor.qty_to_source <= 0:
                label = _("%(amount)s (est.)", amount=label)
            vendor.total_line_cost_display = label

    @api.depends(
        "price", "qty_to_source", "line_id.product_qty",
        "line_id.vendor_line_ids.price", "line_id.vendor_line_ids.qty_to_source",
    )
    def _compute_is_best_total(self):
        for vendor in self:
            totals = [v._get_total_amount() for v in vendor.line_id.vendor_line_ids]
            vendor.is_best_total = bool(totals) and vendor._get_total_amount() == min(totals)

    @api.depends(
        "price", "base_price", "delay", "base_delay",
        "min_qty", "base_min_qty", "payment_term_id", "base_payment_term_id",
    )
    def _compute_change_flags(self):
        """MVS-025 — latest figure differs from its immutable baseline."""
        for vendor in self:
            vendor.price_changed = vendor.price != vendor.base_price
            vendor.delay_changed = vendor.delay != vendor.base_delay
            vendor.min_qty_changed = vendor.min_qty != vendor.base_min_qty
            vendor.payment_term_changed = (
                vendor.payment_term_id != vendor.base_payment_term_id
            )
            vendor.has_change = any((
                vendor.price_changed, vendor.delay_changed,
                vendor.min_qty_changed, vendor.payment_term_changed,
            ))

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
    # CRUD — baseline snapshot (MVS-025) + chatter logging (BR-011 / D-012)
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        """MVS-025 TD-001 — snapshot the row's own figures as the baseline.

        Captured once, post-create, from the record's own values (the seeded
        supplierinfo figures or the inline-entered ones) — never from a live
        supplierinfo lookup, so it is immune to later master-data drift and
        correct for inline-added vendors. base_* are absent from TRACKED_FIELDS,
        so the write() below neither logs nor recurses on them.
        """
        records = super().create(vals_list)
        for vendor in records:
            vendor.base_price = vendor.price
            vendor.base_delay = vendor.delay
            vendor.base_min_qty = vendor.min_qty
            vendor.base_payment_term_id = vendor.payment_term_id.id
        return records

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
