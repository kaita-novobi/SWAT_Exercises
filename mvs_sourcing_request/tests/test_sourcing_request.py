# -*- coding: utf-8 -*-
"""Automated coverage for TDD [MVS-003] §10 (rows marked Automated)."""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSourcingRequest(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        # Reuse a standard payment term to avoid the payment-term line constraint.
        cls.payment_term = cls.env.ref("account.account_payment_term_30days")
        cls.vendor_a = cls.env["res.partner"].create({"name": "Vendor A"})
        cls.vendor_b = cls.env["res.partner"].create({"name": "Vendor B"})
        cls.customer = cls.env["res.partner"].create({"name": "Customer"})

        # Non-stored good so it is never excluded as "in stock".
        cls.product = cls.env["product.product"].create({
            "name": "Sourced Widget",
            "type": "consu",
            "is_storable": False,
            "purchase_ok": True,
            "seller_ids": [
                (0, 0, {"partner_id": cls.vendor_a.id, "price": 100.0,
                        "delay": 5, "min_qty": 1.0}),
                (0, 0, {"partner_id": cls.vendor_b.id, "price": 120.0,
                        "delay": 2, "min_qty": 1.0}),
            ],
        })
        cls.product_no_vendor = cls.env["product.product"].create({
            "name": "Orphan Widget",
            "type": "consu",
            "is_storable": False,
            "purchase_ok": True,
        })
        cls.sale_order = cls.env["sale.order"].create({
            "partner_id": cls.customer.id,
            "order_line": [(0, 0, {
                "product_id": cls.product.id,
                "product_uom_qty": 10.0,
            })],
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _make_request(self):
        self.sale_order.action_request_sourcing()
        return self.sale_order.sourcing_request_ids

    # ------------------------------------------------------------------
    # R01 / BR-007
    # ------------------------------------------------------------------
    def test_r01_seeding(self):
        request = self._make_request()
        self.assertEqual(len(request), 1)
        self.assertEqual(len(request.line_ids), 1)
        line = request.line_ids
        self.assertEqual(line.product_id, self.product)
        self.assertEqual(line.product_qty, 10.0)
        self.assertEqual(len(line.vendor_line_ids), 2)

    def test_br007_no_duplicate_request(self):
        self._make_request()
        with self.assertRaises(UserError):
            self.sale_order.action_request_sourcing()

    # ------------------------------------------------------------------
    # BR-002 / BR-012 — Start guards
    # ------------------------------------------------------------------
    def test_br002_start_requires_routing(self):
        request = self._make_request()
        request.line_ids.routing = False
        with self.assertRaises(UserError):
            request.action_start()

    def test_br012_auto_line_without_vendor(self):
        self.sale_order.order_line = [(0, 0, {
            "product_id": self.product_no_vendor.id,
            "product_uom_qty": 4.0,
        })]
        request = self._make_request()
        orphan_line = request.line_ids.filtered(
            lambda l: l.product_id == self.product_no_vendor
        )
        orphan_line.routing = "auto"
        # Keep the sourced line assisted so only the orphan auto line is checked.
        request.line_ids.filtered(
            lambda l: l.product_id == self.product
        ).routing = "assisted"
        with self.assertRaises(UserError):
            request.action_start()

    # ------------------------------------------------------------------
    # R04 — Start creates a direct draft PO to the preferred vendor
    # (regression for the removed v19 procurement.group API).
    # ------------------------------------------------------------------
    def test_r04_auto_start_creates_direct_po(self):
        request = self._make_request()
        request.line_ids.routing = "auto"
        request.action_start()

        self.assertEqual(request.state, "in_sourcing")
        self.assertEqual(len(request.purchase_order_ids), 1)
        po = request.purchase_order_ids
        self.assertEqual(po.state, "draft")
        # Cheapest seller (Vendor A @ 100) is the preferred vendor.
        self.assertEqual(po.partner_id, self.vendor_a)
        self.assertEqual(po.sourcing_request_id, request)
        self.assertFalse(
            po.sourcing_vendor_line_id,
            "Automatic PO must not be a candidate RFQ (no vendor-line link)",
        )
        self.assertEqual(po.order_line.product_id, self.product)
        self.assertEqual(po.order_line.product_qty, 10.0)

    # ------------------------------------------------------------------
    # BR-005 — qty_to_source bounds
    # ------------------------------------------------------------------
    def test_br005_qty_to_source_bounds(self):
        request = self._make_request()
        vendor = request.line_ids.vendor_line_ids[0]
        with self.assertRaises(ValidationError):
            vendor.qty_to_source = 999.0  # > required (10)
        with self.assertRaises(ValidationError):
            vendor.qty_to_source = -1.0

    # ------------------------------------------------------------------
    # BR-006 — locked after PO Created
    # ------------------------------------------------------------------
    def test_br006_locked_after_po_created(self):
        request = self._make_request()
        request.with_context(skip_sourcing_state_guard=True).write(
            {"state": "po_created"}
        )
        with self.assertRaises(UserError):
            request.write({"origin": "tampered"})
        with self.assertRaises(UserError):
            request.unlink()

    # ------------------------------------------------------------------
    # BR-010 — one-way RFQ -> grid back-sync
    # ------------------------------------------------------------------
    def test_br010_backsync_one_way(self):
        request = self._make_request()
        request.line_ids.routing = "assisted"
        request.action_start()
        self.assertEqual(request.state, "in_sourcing")
        self.assertFalse(request.rfq_created, "No RFQ before Create RFQs")
        request.action_create_rfqs()
        self.assertTrue(request.rfq_created, "rfq_created flips after Create RFQs")

        vendor = request.line_ids.vendor_line_ids.filtered("rfq_id")[0]
        rfq = vendor.rfq_id
        self.assertEqual(rfq.state, "draft")
        po_line = rfq.order_line[0]

        new_date = rfq.date_order + timedelta(days=9)
        rfq.write({
            "payment_term_id": self.payment_term.id,
            "order_line": [(1, po_line.id, {
                "price_unit": 55.0,
                "date_planned": new_date,
            })],
        })

        self.assertEqual(vendor.price, 55.0)
        self.assertEqual(vendor.delay, 9)
        self.assertEqual(vendor.payment_term_id, self.payment_term)

    def test_br010_unrelated_po_not_synced(self):
        # A PO with no sourcing link must pass straight through write().
        po = self.env["purchase.order"].create({
            "partner_id": self.vendor_a.id,
            "order_line": [(0, 0, {
                "product_id": self.product.id,
                "product_qty": 3.0,
                "price_unit": 10.0,
                "product_uom_id": self.product.uom_id.id,
                "name": self.product.display_name,
            })],
        })
        self.assertFalse(po.sourcing_vendor_line_id)
        po.order_line[0].price_unit = 12.0  # must not raise
        self.assertEqual(po.order_line[0].price_unit, 12.0)

    # ------------------------------------------------------------------
    # R06/R07 — Create POs with a mixed Automatic + Assisted request
    # Regression: Automatic lines carry seeded candidate rows and Automatic
    # procurement POs carry sourcing_request_id but no sourcing_vendor_line_id.
    # Creating POs must NOT (a) raise BR-003 for the Automatic line, nor
    # (b) cancel the Automatic procurement PO as a "loser".
    # ------------------------------------------------------------------
    def test_create_pos_mixed_auto_assisted(self):
        # Second product for the Automatic line (purchasable, has a vendor).
        product_auto = self.env["product.product"].create({
            "name": "Auto Widget",
            "type": "consu",
            "is_storable": False,
            "purchase_ok": True,
            "seller_ids": [(0, 0, {"partner_id": self.vendor_a.id,
                                   "price": 50.0, "delay": 3, "min_qty": 1.0})],
        })
        self.sale_order.order_line = [(0, 0, {
            "product_id": product_auto.id,
            "product_uom_qty": 5.0,
        })]
        request = self._make_request()

        assisted_line = request.line_ids.filtered(
            lambda l: l.product_id == self.product)
        auto_line = request.line_ids.filtered(
            lambda l: l.product_id == product_auto)
        assisted_line.routing = "assisted"
        auto_line.routing = "auto"
        self.assertTrue(auto_line.vendor_line_ids,
                        "Automatic line is still seeded with candidate rows")

        # Move to in_sourcing without invoking real procurement, then create
        # the Assisted RFQs and simulate the Automatic procurement PO.
        request.state = "in_sourcing"
        request.action_create_rfqs()
        auto_po = self.env["purchase.order"].create({
            "partner_id": self.vendor_a.id,
            "sourcing_request_id": request.id,
            "order_line": [(0, 0, {
                "product_id": product_auto.id,
                "product_qty": 5.0,
                "price_unit": 50.0,
                "product_uom_id": product_auto.uom_id.id,
                "name": product_auto.display_name,
            })],
        })
        self.assertEqual(auto_po.state, "draft")

        # Pick the cheaper assisted vendor and allocate the full qty.
        winner = assisted_line.vendor_line_ids.sorted("price")[0]
        winner.selected = True
        winner.qty_to_source = assisted_line.product_qty

        request.with_context(confirm_allocation=True).action_create_purchase_orders()

        self.assertEqual(request.state, "po_created")
        # (b) Automatic PO survives.
        self.assertNotEqual(auto_po.state, "cancel",
                            "Automatic procurement PO must not be cancelled")
        # Winner confirmed, the other candidate RFQ cancelled.
        self.assertEqual(winner.rfq_id.state, "purchase")
        loser = (assisted_line.vendor_line_ids - winner)
        self.assertEqual(loser.rfq_id.state, "cancel")

    # ------------------------------------------------------------------
    # Allocation counts only SELECTED candidate vendor lines
    # ------------------------------------------------------------------
    def test_allocated_counts_selected_only(self):
        request = self._make_request()
        line = request.line_ids  # required qty = 10
        vendor_a, vendor_b = line.vendor_line_ids[0], line.vendor_line_ids[1]

        vendor_a.write({"qty_to_source": 6.0, "selected": True})
        vendor_b.write({"qty_to_source": 4.0, "selected": False})
        self.assertEqual(line.allocated_qty, 6.0, "Only the selected row counts")
        self.assertFalse(line.is_fully_allocated)

        vendor_b.selected = True
        self.assertEqual(line.allocated_qty, 10.0)
        self.assertTrue(line.is_fully_allocated)

    # ------------------------------------------------------------------
    # F2 — can_create_po drives the Create Purchase Orders button visibility
    # ------------------------------------------------------------------
    def test_f2_can_create_po_flag(self):
        request = self._make_request()
        request.line_ids.routing = "assisted"
        self.assertFalse(request.can_create_po, "No selection yet")
        vendor = request.line_ids.vendor_line_ids.sorted("price")[0]
        vendor.selected = True
        self.assertFalse(request.can_create_po, "Selected but qty still 0")
        vendor.qty_to_source = request.line_ids.product_qty
        self.assertTrue(request.can_create_po, "Selected with positive qty")

    # ------------------------------------------------------------------
    # BR-008 — action guards refuse a user without purchasing rights
    # (§10 Automated). ACL grants sourcing.request only to sales/purchase
    # groups; a plain internal user gets AccessError on the lifecycle actions.
    # ------------------------------------------------------------------
    def test_br008_unauthorized_user_blocked(self):
        request = self._make_request()
        plain_user = self.env["res.users"].create({
            "name": "No Access",
            "login": "no_access_mvs003",
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        with self.assertRaises(AccessError):
            request.with_user(plain_user).action_start()

    # ==================================================================
    # MVS-024 — vendor comparison enhancements (A1 / B1 read-outs)
    # ==================================================================
    def test_r01_total_line_cost(self):
        request = self._make_request()
        vendor = request.line_ids.vendor_line_ids.sorted("price")[0]  # A @ 100
        # Before allocation: estimate on required qty (10).
        self.assertEqual(vendor._get_total_amount(), 1000.0)
        self.assertIn("(est.)", vendor.total_line_cost_display)
        # After allocation: price × Qty to Source.
        vendor.qty_to_source = 4.0
        self.assertEqual(vendor._get_total_amount(), 400.0)
        self.assertNotIn("(est.)", vendor.total_line_cost_display)

    def test_r02_is_best_total(self):
        request = self._make_request()
        vendors = request.line_ids.vendor_line_ids.sorted("price")
        self.assertTrue(vendors[0].is_best_total)   # A @ 100 → lowest total
        self.assertFalse(vendors[1].is_best_total)  # B @ 120

    def test_r03_estimated_total_spend(self):
        request = self._make_request()
        self.assertEqual(request.estimated_total_spend, 0.0)
        vendors = request.line_ids.vendor_line_ids.sorted("price")
        vendors[0].write({"selected": True, "qty_to_source": 4.0})
        self.assertEqual(request.estimated_total_spend, 400.0)
        vendors[1].write({"selected": True, "qty_to_source": 6.0})
        self.assertEqual(request.estimated_total_spend, 1120.0)  # 4×100 + 6×120

    def test_r04_sourced_awaiting_counts(self):
        so = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [
                (0, 0, {"product_id": self.product.id, "product_uom_qty": 10.0}),
                (0, 0, {"product_id": self.product_no_vendor.id,
                        "product_uom_qty": 4.0}),
            ],
        })
        so.action_request_sourcing()
        request = so.sourcing_request_ids
        self.assertEqual(len(request.line_ids), 2)
        sourced_line = request.line_ids.filtered(
            lambda l: l.product_id == self.product)
        sourced_line.vendor_line_ids.sorted("price")[0].write(
            {"selected": True, "qty_to_source": 10.0})
        self.assertEqual(request.lines_sourced_count, 1)
        self.assertEqual(request.lines_awaiting_count, 1)
        self.assertIn("1 sourced / 1 awaiting", request.sourcing_progress_display)

    def test_r05_deadline_risk(self):
        self.sale_order.commitment_date = fields.Datetime.now() + timedelta(days=1)
        request = self._make_request()
        # Unsourced line: earliest candidate lead time (2d) > 1-day deadline → risk.
        self.assertEqual(request.deadline_risk_count, 1)
        self.assertEqual(request.deadline_risk_display, "1")
        # No Shipping Date → em dash, no risk.
        self.sale_order.commitment_date = False
        self.assertEqual(request.deadline_risk_count, 0)
        self.assertEqual(request.deadline_risk_display, "—")

    # ------------------------------------------------------------------
    # §6.2 — multi-company record rules (all three models)
    # ------------------------------------------------------------------
    def test_record_rules_multi_company(self):
        request = self._make_request()
        line = request.line_ids
        vendor = line.vendor_line_ids[0]

        company_b = self.env["res.company"].create({"name": "Company B"})
        user_b = self.env["res.users"].create({
            "name": "Buyer B",
            "login": "buyer_b_mvs003",
            "company_id": company_b.id,
            "company_ids": [(6, 0, [company_b.id])],
            "group_ids": [(4, self.env.ref("purchase.group_purchase_user").id)],
        })

        self.assertFalse(
            request.with_user(user_b).search([("id", "=", request.id)]),
            "Company-B user must not see Company-A sourcing request",
        )
        self.assertFalse(
            line.with_user(user_b).search([("id", "=", line.id)]),
            "Company-B user must not see Company-A sourcing line",
        )
        self.assertFalse(
            vendor.with_user(user_b).search([("id", "=", vendor.id)]),
            "Company-B user must not see Company-A candidate vendor",
        )
