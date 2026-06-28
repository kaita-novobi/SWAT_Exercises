# -*- coding: utf-8 -*-
"""Sourcing Request Line — one product to source within a request (TDD §3.1)."""

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.tools import float_compare


class SourcingRequestLine(models.Model):
    _name = "sourcing.request.line"
    _description = "Sourcing Request Line"
    _rec_name = "product_id"
    _order = "id"

    request_id = fields.Many2one(
        "sourcing.request", string="Request", required=True, index=True,
        ondelete="cascade",
    )
    product_id = fields.Many2one(
        "product.product", string="Product", required=True, index=True,
        ondelete="restrict",
    )
    product_qty = fields.Float(
        string="Required Qty", required=True, digits="Product Unit of Measure",
    )
    sale_line_id = fields.Many2one(
        "sale.order.line", string="SO Line", index=True, ondelete="cascade",
    )
    # Suggestion only, editable until Start. Enforced as required at Start
    # (BR-002), not at the ORM, so the rule stays observable/testable.
    routing = fields.Selection(
        selection=[("auto", "Automatic"), ("assisted", "Assisted")],
        string="Routing", default="auto",
        help="Automatic triggers native procurement; Assisted opens the RFQ "
             "negotiation flow. Editable until the request is started.",
    )
    vendor_line_ids = fields.One2many(
        "sourcing.request.line.vendor", "line_id", string="Candidate Vendors",
    )
    allocated_qty = fields.Float(
        string="Allocated", compute="_compute_allocated",
        digits="Product Unit of Measure",
    )
    is_fully_allocated = fields.Boolean(
        string="Fully Allocated", compute="_compute_allocated",
    )
    # MVS-024 B1 — line-level inputs the request summary header aggregates.
    is_sourced = fields.Boolean(
        string="Sourced", compute="_compute_is_sourced",
        help="D-004 — the line has at least one selected vendor with Qty to "
             "Source > 0.",
    )
    is_deadline_risk = fields.Boolean(
        string="Deadline Risk", compute="_compute_is_deadline_risk",
        help="D-003 — expected arrival (today + Lead Time) is later than the "
             "request Shipping Date.",
    )

    @api.depends("vendor_line_ids.qty_to_source", "vendor_line_ids.selected", "product_qty")
    def _compute_allocated(self):
        for line in self:
            # Only selected candidate vendors count toward the allocation.
            allocated = sum(
                line.vendor_line_ids.filtered("selected").mapped("qty_to_source")
            )
            line.allocated_qty = allocated
            rounding = line.product_id.uom_id.rounding or 0.01
            line.is_fully_allocated = (
                float_compare(allocated, line.product_qty, precision_rounding=rounding) == 0
            )

    @api.depends("vendor_line_ids.selected", "vendor_line_ids.qty_to_source")
    def _compute_is_sourced(self):
        for line in self:
            line.is_sourced = any(
                v.selected and v.qty_to_source > 0 for v in line.vendor_line_ids
            )

    @api.depends(
        "is_sourced", "request_id.shipping_date",
        "vendor_line_ids.selected", "vendor_line_ids.qty_to_source",
        "vendor_line_ids.delay",
    )
    def _compute_is_deadline_risk(self):
        for line in self:
            shipping = line.request_id.shipping_date
            if not shipping:
                line.is_deadline_risk = False
                continue
            if line.is_sourced:
                # Latest expected arrival among the selected winners.
                delays = line.vendor_line_ids.filtered(
                    lambda v: v.selected and v.qty_to_source > 0
                ).mapped("delay")
            else:
                # Early warning: earliest (best-case) candidate arrival (D-003).
                delays = line.vendor_line_ids.mapped("delay")
            if not delays:
                line.is_deadline_risk = False
                continue
            arrival_delay = max(delays) if line.is_sourced else min(delays)
            arrival = fields.Date.context_today(line) + timedelta(days=arrival_delay)
            line.is_deadline_risk = arrival > shipping.date()

    @api.onchange("product_id")
    def _onchange_product_id_routing(self):
        for line in self:
            if line.product_id:
                line.routing = line._suggest_routing(line.product_id)

    @api.model
    def _suggest_routing(self, product):
        """§4.2/D-014 — suggest 'assisted' when no usable or stale supplierinfo."""
        seller = product._select_seller() if product else product.browse()
        if not seller:
            return "assisted"
        stale_days = int(
            self.env["ir.config_parameter"].sudo().get_param("mvs_sourcing.stale_days", 90)
        )
        if seller.write_date and (fields.Datetime.now() - seller.write_date).days > stale_days:
            return "assisted"
        return "auto"

    def _get_preferred_seller(self):
        """Best supplierinfo for the line's product/qty (BR-012)."""
        self.ensure_one()
        if not self.product_id:
            return self.env["product.supplierinfo"]
        return self.product_id._select_seller(
            quantity=self.product_qty, uom_id=self.product_id.uom_id
        )
