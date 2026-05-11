# -*- coding: utf-8 -*-
"""
purchase_order_line.py
======================
Overrides purchase.order.line._compute_date_planned to use the company's
Resource Calendar when computing the scheduled receipt date.

Business Rule (FR-01):
    Receipt Date = Confirmation Date + Lead Time (working days only)

    - Confirmation Date : purchase.order.date_order
    - Lead Time         : product.supplierinfo.delay  (in working days)
    - Working days      : defined by res.company.resource_calendar_id,
                          excluding weekends and resource.calendar.leaves
                          (public holidays)

Fallback chain (FR-03 / FR-04):
    delay == 0          → return date_order unchanged
    no resource calendar → add timedelta(days=delay) (standard behaviour)
    plan_days() fails   → add timedelta(days=delay) (safety net)
"""

import logging
from datetime import timedelta

import pytz

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class PurchaseOrderLine(models.Model):
    """Extends purchase.order.line to compute date_planned using the company's
    Resource Calendar, skipping weekends and public holidays.

    Inherits: purchase.order.line
    """

    _inherit = "purchase.order.line"

    # ─────────────────────────────────────────────────────────────────────────
    # Compute overrides
    # ─────────────────────────────────────────────────────────────────────────

    @api.depends(
        "product_id",
        "order_id.date_order",
        "order_id.date_planned",
        "product_qty",
        "product_uom",
        "order_id.company_id",
    )
    def _compute_date_planned(self):
        """Override: compute date_planned using working-day arithmetic.

        For each line the method resolves the vendor (seller), retrieves the
        company's Resource Calendar, and delegates to the helper
        `_get_planned_date_with_calendar` to add the lead time as working days.

        FR-01 – Receipt Date = Confirmation Date + Lead Time (working days only)
        FR-05 – Triggered by: date_order, product_id, product_qty,
                              product_uom, company_id
        """
        for line in self:
            seller = line._get_seller()
            date_order = line.order_id.date_order or fields.Datetime.now()
            company = line.order_id.company_id or self.env.company
            line.date_planned = line._get_planned_date_with_calendar(
                seller, date_order, company
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Helper methods
    # ─────────────────────────────────────────────────────────────────────────

    def _get_planned_date_with_calendar(self, seller, date_order, company):
        """Compute the planned receipt date by adding lead-time working days.

        Uses `resource.calendar.plan_days()` with `compute_leaves=True` so that
        both non-working days (weekends / calendar attendance) and public
        holidays (resource.calendar.leaves) are automatically excluded.

        Args:
            seller  (product.supplierinfo | EmptyRecordset):
                        The matched vendor pricelist entry, or an empty recordset
                        when no vendor is found.  The `delay` attribute gives the
                        lead time in working days.
            date_order (datetime):
                        The PO confirmation date (naive UTC, as stored by Odoo).
            company (res.company):
                        The company whose Resource Calendar should be used.

        Returns:
            datetime: Naive UTC datetime representing the planned receipt date.

        Raises:
            No exceptions are raised; all error paths fall back gracefully.

        Fallback chain (FR-03 / FR-04):
            - delay == 0                 → return date_order as-is
            - no resource calendar        → date_order + timedelta(days=delay)
            - plan_days() returns falsy   → date_order + timedelta(days=delay)
        """
        delay = seller.delay if seller else 0

        # FR-04: zero lead time — no calculation needed
        if not delay:
            return date_order

        calendar = company.resource_calendar_id

        # FR-03: no calendar configured — standard calendar-day fallback
        if not calendar:
            _logger.debug(
                "actual_vendor_lead_time: company '%s' has no resource calendar; "
                "falling back to %d calendar day(s) from %s.",
                company.name,
                delay,
                date_order,
            )
            return date_order + timedelta(days=delay)

        # plan_days() requires a timezone-aware datetime.
        # Odoo stores Datetime values as naive UTC, so we localise to UTC first.
        aware_dt = (
            date_order
            if date_order.tzinfo
            else pytz.utc.localize(date_order)
        )

        try:
            # add `delay` working days, honouring public holidays
            planned_aware = calendar.plan_days(
                delay, aware_dt, compute_leaves=True
            )
        except Exception:
            _logger.exception(
                "actual_vendor_lead_time: plan_days() failed for calendar '%s' "
                "(id=%s); falling back to %d calendar day(s) from %s.",
                calendar.name,
                calendar.id,
                delay,
                date_order,
            )
            return date_order + timedelta(days=delay)

        if not planned_aware:
            _logger.warning(
                "actual_vendor_lead_time: plan_days() returned no result for "
                "calendar '%s' (id=%s) and delay=%d; "
                "falling back to calendar days.",
                calendar.name,
                calendar.id,
                delay,
            )
            return date_order + timedelta(days=delay)

        # Convert tz-aware result back to naive UTC (Odoo's Datetime convention)
        return planned_aware.astimezone(pytz.utc).replace(tzinfo=None)
