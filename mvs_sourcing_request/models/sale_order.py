# -*- coding: utf-8 -*-
"""sale.order extension — Request Sourcing action and smart button (R01/R08)."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    sourcing_request_ids = fields.One2many(
        "sourcing.request", "sale_order_id", string="Sourcing Requests",
    )
    sourcing_request_count = fields.Integer(
        string="Sourcing Request Count", compute="_compute_sourcing_request_count",
    )

    @api.depends("sourcing_request_ids")
    def _compute_sourcing_request_count(self):
        for order in self:
            order.sourcing_request_count = len(order.sourcing_request_ids)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_request_sourcing(self):
        """R01/MVS-005 — create a sourcing request seeded from the SO lines.

        Children (lines + candidate vendor rows) are seeded via ``sudo()`` so a
        salesman, who holds Create only on ``sourcing.request``, can raise the
        request (TQ-001 / §6.1).
        """
        self.ensure_one()

        # BR-007 — only one live request per SO.
        if self.sourcing_request_ids.filtered(lambda r: r.state != "cancel"):
            raise UserError(
                _("A sourcing request already exists for %s.", self.name)
            )

        line_commands = [
            (0, 0, self._prepare_sourcing_line_vals(so_line))
            for so_line in self.order_line
            if self._sourcing_line_needed(so_line)
        ]
        if not line_commands:
            raise UserError(
                _("There is no purchasable line to source on %s.", self.name)
            )

        request = self.env["sourcing.request"].sudo().create({
            "sale_order_id": self.id,
            "origin": self.name,
            "company_id": self.company_id.id,
            "user_id": self.env.user.id,
            "line_ids": line_commands,
        })
        return {
            "type": "ir.actions.act_window",
            "name": _("Sourcing Request"),
            "res_model": "sourcing.request",
            "view_mode": "form",
            "res_id": request.id,
        }

    def action_view_sourcing_requests(self):
        self.ensure_one()
        requests = self.sourcing_request_ids
        action = {
            "type": "ir.actions.act_window",
            "name": _("Sourcing Requests"),
            "res_model": "sourcing.request",
            "domain": [("id", "in", requests.ids)],
            "view_mode": "list,form",
        }
        if len(requests) == 1:
            action.update(view_mode="form", res_id=requests.id)
        return action

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _sourcing_line_needed(self, so_line):
        """Purchasable line that is not already covered by stock (R01)."""
        product = so_line.product_id
        if so_line.display_type or not product:
            return False
        if not product.purchase_ok:
            return False
        # Skip storable products already on hand (in-stock excluded).
        if product.is_storable and product.qty_available >= so_line.product_uom_qty:
            return False
        return True

    def _prepare_sourcing_line_vals(self, so_line):
        product = so_line.product_id
        vendor_commands = [
            (0, 0, {
                "partner_id": seller.partner_id.id,
                "price": seller.price,
                "delay": seller.delay,
                "min_qty": seller.min_qty,
                "currency_id": (seller.currency_id or self.company_id.currency_id).id,
                "payment_term_id": seller.partner_id.with_company(
                    self.company_id
                ).property_supplier_payment_term_id.id or False,
            })
            for seller in product.seller_ids
        ]
        return {
            "product_id": product.id,
            "product_qty": so_line.product_uom_qty,
            "sale_line_id": so_line.id,
            "routing": self.env["sourcing.request.line"]._suggest_routing(product),
            "vendor_line_ids": vendor_commands,
        }
