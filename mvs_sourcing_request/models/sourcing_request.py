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
    can_create_po = fields.Boolean(
        string="Has Selected Winner", compute="_compute_can_create_po",
        help="True once at least one Assisted candidate vendor is selected with "
             "a positive Qty to Source — drives the Create Purchase Orders button "
             "(F2 / FDD §4 'Enabled When').",
    )

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------
    @api.depends("purchase_order_ids")
    def _compute_po_count(self):
        for request in self:
            request.po_count = len(request.purchase_order_ids)

    @api.depends(
        "line_ids.routing",
        "line_ids.vendor_line_ids.selected",
        "line_ids.vendor_line_ids.qty_to_source",
    )
    def _compute_can_create_po(self):
        for request in self:
            winners = request.line_ids.filtered(
                lambda l: l.routing == "assisted"
            ).vendor_line_ids.filtered(
                lambda v: v.selected and v.qty_to_source > 0
            )
            request.can_create_po = bool(winners)

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

        # Only Assisted lines take part in vendor selection / RFQ confirmation.
        # Automatic lines were already procured at Start; their seeded candidate
        # rows are never selected and must not be treated as unresolved (BR-003).
        assisted_lines = self.line_ids.filtered(lambda l: l.routing == "assisted")
        lines_with_vendors = assisted_lines.filtered(lambda l: l.vendor_line_ids)

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

        # Losers are the non-selected candidate RFQs only. Automatic-procurement
        # POs carry a sourcing_request_id but NO sourcing_vendor_line_id, so they
        # are excluded here and survive (they must not be cancelled).
        candidate_rfqs = self.purchase_order_ids.filtered("sourcing_vendor_line_id")
        losers = (candidate_rfqs - winners.mapped("rfq_id")).filtered(
            lambda po: po.state in ("draft", "sent")
        )

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
                    "product_uom_id": line.product_id.uom_id.id,
                    "name": line.product_id.display_name,
                }),
            ],
        }

    def _run_auto_procurement(self, auto_lines):
        """R04 — create a draft PO per preferred vendor for the Automatic lines.

        Direct-PO realisation of the Automatic path (approved deviation from
        TDD §4.2; deep native procurement / MTO is deferred to MVS-019 per the
        FDD). One draft ``purchase.order`` per preferred vendor, seeded from the
        product's best ``supplierinfo`` (Automatic lines to the same vendor are
        merged into one PO). The buyer confirms the PO manually.

        Auto POs carry ``sourcing_request_id`` for traceability but NO
        ``sourcing_vendor_line_id`` — they are not candidate RFQs and must never
        be treated as losers in :meth:`action_create_purchase_orders`.
        """
        PurchaseOrder = self.env["purchase.order"]
        date_order = fields.Datetime.now()

        # Group Automatic lines by their preferred vendor (BR-012 already
        # guaranteed every Automatic line has a usable seller).
        by_vendor = {}
        for line in auto_lines:
            seller = line._get_preferred_seller()
            if not seller:
                continue
            by_vendor.setdefault(seller.partner_id, []).append((line, seller))

        created = PurchaseOrder
        for partner, line_sellers in by_vendor.items():
            first_seller = line_sellers[0][1]
            payment_term = partner.with_company(
                self.company_id
            ).property_supplier_payment_term_id
            order_lines = [
                (0, 0, {
                    "product_id": line.product_id.id,
                    "product_qty": line.product_qty,
                    "price_unit": seller.price,
                    # TD-003: date_planned = order date + seller lead time.
                    "date_planned": date_order + timedelta(days=seller.delay or 0),
                    "product_uom_id": line.product_id.uom_id.id,
                    "name": line.product_id.display_name,
                })
                for line, seller in line_sellers
            ]
            order = PurchaseOrder.create({
                "partner_id": partner.id,
                "user_id": (partner.buyer_id or self.env.user).id,
                "company_id": self.company_id.id,
                "currency_id": (
                    first_seller.currency_id or self.company_id.currency_id
                ).id,
                "payment_term_id": payment_term.id or False,
                "date_order": date_order,
                "origin": self.name,
                "sourcing_request_id": self.id,
                "order_line": order_lines,
            })
            created |= order
        return created

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
