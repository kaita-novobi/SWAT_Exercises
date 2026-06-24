# -*- coding: utf-8 -*-
"""Automated coverage for TDD [MVS-003] §10 (rows marked Automated)."""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
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
        request.action_create_rfqs()

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
