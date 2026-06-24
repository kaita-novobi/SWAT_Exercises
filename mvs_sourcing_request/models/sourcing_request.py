# -*- coding: utf-8 -*-
"""Sourcing Request — coordinates multi-vendor sourcing for one Sales Order.

See TDD [MVS-003] v0.1 §3.1 (data model) and §4 (business logic).
"""
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Business fields a user must not change once POs exist (BR-006). System writes
# (state transitions, the related shipping_date, mail/activity fields) stay open.
_PROTECTED_FIELDS = {
    "sale_order_id", "origin", "user_id", "company_id", "line_ids",
}


class SourcingRequest(models.Model):
    _name = "sourcing.request"
    _description = "Sourcing Request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "name"
    _order = "create_date desc"

    name = fields.Char(
        string="Reference", required=True, copy=False, default="New", readonly=True
    )
    sale_order_id = fields.Many2one(
        "sale.order", string="Sales Order", required=True, index=True,
        ondelete="cascade", copy=False,
    )
    origin = fields.Char(string="Origin", copy=False, help="Traceability label.")
    user_id = fields.Many2one(
        "res.users", string="Buyer", required=True, ondelete="restrict",
        default=lambda self: self.env.user, tracking=True,
    )
    company_id = fields.Many2one(
        "res.company", string="Company", required=True, index=True,
        ondelete="restrict", default=lambda self: self.env.company,
    )
    shipping_date = fields.Datetime(
        string="Shipping Date", related="sale_order_id.commitment_date",
        readonly=True, store=True,
        help="Delivery date promised on the Sales Order — deadline context only.",
    )
    state = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("in_sourcing", "In Sourcing"),
            ("selected", "Selected"),
            ("po_created", "PO Created"),
            ("done", "Done"),
            ("cancel", "Cancelled"),
        ],
        string="Status", default="draft", required=True, copy=False, tracking=True,
    )
    line_ids = fields.One2many(
        "sourcing.request.line", "request_id", string="Lines",
    )
    purchase_order_ids = fields.One2many(
        "purchase.order", "sourcing_request_id", string="Purchase Orders",
    )
    po_count = fields.Integer(
        string="PO Count", compute="_compute_po_count",
    )

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------
    @api.depends("purchase_order_ids")
    def _compute_po_count(self):
        for request in self:
            request.po_count = len(request.purchase_order_ids)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("sourcing.request") or "New"
                )
        return super().create(vals_list)

    def write(self, vals):
        # BR-006: no edit of protected business fields once POs are created.
        if not self.env.context.get("skip_sourcing_state_guard") and (
            _PROTECTED_FIELDS & set(vals.keys())
        ):
            locked = self.filtered(lambda r: r.state in ("po_created", "done"))
            if locked:
                raise UserError(
                    _(
                        "Sourcing request %s can no longer be modified — its "
                        "purchase orders have already been created.",
                        ", ".join(locked.mapped("name")),
                    )
                )
        return super().write(vals)

    def unlink(self):
        # BR-006: protect created-PO requests from deletion.
        locked = self.filtered(lambda r: r.state in ("po_created", "done"))
        if locked:
            raise UserError(
                _(
                    "Sourcing request %s cannot be deleted after its purchase "
                    "orders have been created.",
                    ", ".join(locked.mapped("name")),
                )
            )
        return super().unlink()

    # ------------------------------------------------------------------
    # Actions — lifecycle (R03..R07)
    # ------------------------------------------------------------------
    def action_start(self):
        """R03/R04 — split Automatic vs Assisted; trigger native procurement.

        BR-002: blocked until every line has a routing.
        BR-012: an Automatic line must have a usable supplierinfo.
        """
        self.ensure_one()
        if self.state != "draft":
            raise UserError(_("Only a draft request can be started."))
        if not self.line_ids:
            raise UserError(_("Add at least one line before starting the request."))

        # BR-002 — every line must have a routing.
        missing_routing = self.line_ids.filtered(lambda l: not l.routing)
        if missing_routing:
            raise UserError(
                _(
                    "Set a routing on every line before starting. Missing on: %s",
                    ", ".join(missing_routing.mapped("product_id.display_name")),
                )
            )

        auto_lines = self.line_ids.filtered(lambda l: l.routing == "auto")

        # BR-012 — Automatic lines need a usable vendor.
        no_vendor = auto_lines.filtered(lambda l: not l._get_preferred_seller())
        if no_vendor:
            raise UserError(
                _(
                    "These Automatic lines have no usable vendor; switch them to "
                    "Assisted or add a vendor: %s",
                    ", ".join(no_vendor.mapped("product_id.display_name")),
                )
            )

        if auto_lines:
            self._run_auto_procurement(auto_lines)

        self.state = "in_sourcing"
        self.message_post(body=_("Sourcing started."))
        return True

    def action_create_rfqs(self):
        """R05 — one draft RFQ per shortlisted Assisted vendor row."""
        self.ensure_one()
        if self.state != "in_sourcing":
            raise UserError(_("RFQs can only be created while the request is in sourcing."))

        PurchaseOrder = self.env["purchase.order"]
        assisted_lines = self.line_ids.filtered(lambda l: l.routing == "assisted")
        created = self.env["purchase.order"]

        for line in assisted_lines:
            shortlist = line.vendor_line_ids.filtered(lambda v: not v.rfq_id)
            if not shortlist:
                # BR-009: assisted line with no vendor row -> skip (warned elsewhere).
                continue
            for vendor in shortlist:
                order = PurchaseOrder.create(self._prepare_rfq_vals(vendor))
                vendor.rfq_id = order.id
                created |= order

        if not created:
            return self._notify(
                _("No new RFQ to create — every Assisted line already has its RFQs.")
            )
        self.message_post(body=_("%s draft RFQ(s) created.", len(created)))
        return self.action_view_purchase_orders()

    def action_create_purchase_orders(self):
        """R06/R07 — confirm the selected RFQs into POs and cancel the losers.

        TD-001 RFQ reuse: write the final grid figures onto each selected
        vendor's existing draft ``rfq_id`` and confirm it; cancel every
        non-selected draft RFQ on the request. Idempotent via the state guard.
        """
        self.ensure_one()
        if self.state not in ("in_sourcing", "selected"):
            raise UserError(
                _("Purchase orders can only be created from a sourcing request that is in progress.")
            )

        lines_with_vendors = self.line_ids.filtered(lambda l: l.vendor_line_ids)

        # BR-003 — a line offering candidates must have a winner.
        unresolved = lines_with_vendors.filtered(
            lambda l: not l.vendor_line_ids.filtered("selected")
        )
        if unresolved:
            raise UserError(
                _(
                    "Select a winning vendor on every line that has candidates. "
                    "Pending: %s",
                    ", ".join(unresolved.mapped("product_id.display_name")),
                )
            )

        # BR-001 — partial allocation is allowed but must be acknowledged.
        if not self.env.context.get("confirm_allocation"):
            partial = lines_with_vendors.filtered(lambda l: not l.is_fully_allocated)
            if partial:
                return self._notify(
                    _(
                        "Some lines are not fully allocated (%s). Re-run with "
                        "confirmation to proceed anyway.",
                        ", ".join(partial.mapped("product_id.display_name")),
                    )
                )

        selected_vendors = lines_with_vendors.mapped("vendor_line_ids").filtered("selected")
        winners = selected_vendors.filtered(lambda v: v.rfq_id and v.rfq_id.state in ("draft", "sent"))
        if not winners:
            raise UserError(
                _("Selected vendors have no draft RFQ to confirm. Create RFQs first.")
            )

        losers = (
            self.purchase_order_ids
            - winners.mapped("rfq_id")
        ).filtered(lambda po: po.state in ("draft", "sent"))

        # Push final grid figures onto the winning RFQs, then confirm.
        for vendor in winners:
            vendor._apply_to_rfq()
        winners.mapped("rfq_id").with_context(skip_sourcing_sync=True).button_confirm()

        if losers:
            losers.button_cancel()

        self.state = "po_created"
        self.message_post(
            body=_(
                "%(win)s purchase order(s) confirmed, %(lose)s RFQ(s) cancelled.",
                win=len(winners),
                lose=len(losers),
            )
        )
        return self.action_view_purchase_orders()

    def action_view_purchase_orders(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("purchase.purchase_rfq")
        orders = self.purchase_order_ids
        action["domain"] = [("id", "in", orders.ids)]
        action["context"] = {"default_sourcing_request_id": self.id}
        if len(orders) == 1:
            form = self.env.ref("purchase.purchase_order_form")
            action["views"] = [(form.id, "form")]
            action["res_id"] = orders.id
        return action

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _prepare_rfq_vals(self, vendor):
        """Build the create() vals for one draft RFQ from a vendor row (R05)."""
        self.ensure_one()
        line = vendor.line_id
        date_order = fields.Datetime.now()
        # TD-003: date_planned = date_order + delay days
        date_planned = date_order + timedelta(days=vendor.delay or 0)
        return {
            "partner_id": vendor.partner_id.id,
            "user_id": (vendor.partner_id.buyer_id or self.env.user).id,
            "company_id": self.company_id.id,
            "currency_id": (vendor.currency_id or self.company_id.currency_id).id,
            "payment_term_id": vendor.payment_term_id.id or False,
            "date_order": date_order,
            "origin": self.name,
            "sourcing_request_id": self.id,
            "sourcing_vendor_line_id": vendor.id,
            "order_line": [
                (0, 0, {
                    "product_id": line.product_id.id,
                    "product_qty": vendor.qty_to_source or line.product_qty,
                    "price_unit": vendor.price,
                    "date_planned": date_planned,
                    "product_uom_id": line.product_id.uom_po_id.id,
                    "name": line.product_id.display_name,
                }),
            ],
        }

    def _run_auto_procurement(self, auto_lines):
        """R04 — trigger native buy procurement for Automatic lines.

        Runs the standard Buy route so Odoo creates/merges the draft RFQ and
        links the stock moves, then stamps the resulting PO(s) back onto this
        request for traceability. ``procurement.group.run`` raises a UserError
        of its own if no Buy rule can be resolved (no route / no warehouse).
        """
        ProcurementGroup = self.env["procurement.group"]
        buy_route = self.env.ref(
            "purchase_stock.route_warehouse0_buy", raise_if_not_found=False
        )
        if not buy_route:
            raise UserError(_("The standard Buy route is not available on this database."))
        warehouse = self.env["stock.warehouse"].search(
            [("company_id", "=", self.company_id.id)], limit=1
        )
        if not warehouse:
            raise UserError(
                _("No warehouse is configured for company %s.", self.company_id.display_name)
            )

        group = ProcurementGroup.create({
            "name": self.name,
            "partner_id": self.sale_order_id.partner_id.id,
        })
        date_planned = fields.Datetime.to_string(
            self.shipping_date or fields.Datetime.now()
        )
        procurements = []
        for line in auto_lines:
            product = line.product_id
            procurements.append(ProcurementGroup.Procurement(
                product,
                line.product_qty,
                product.uom_id,
                warehouse.lot_stock_id,
                line.product_id.display_name,
                self.name,
                self.company_id,
                {
                    "company_id": self.company_id,
                    "group_id": group,
                    "date_planned": date_planned,
                    "date_deadline": date_planned,
                    "route_ids": buy_route,
                    "warehouse_id": warehouse,
                },
            ))

        existing = self.env["purchase.order"].search([
            ("origin", "=", self.name),
            ("state", "=", "draft"),
        ])
        ProcurementGroup.run(procurements)
        created = self.env["purchase.order"].search([
            ("origin", "like", self.name),
            ("state", "=", "draft"),
            ("sourcing_request_id", "=", False),
        ]) - existing
        if created:
            created.write({"sourcing_request_id": self.id})

    def _notify(self, message):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Sourcing Request"),
                "message": message,
                "type": "warning",
                "sticky": False,
            },
        }
