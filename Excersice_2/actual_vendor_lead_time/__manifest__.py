# -*- coding: utf-8 -*-
##############################################################################
# Module: actual_vendor_lead_time
# Purpose: Uses the company's Resource Calendar and Public Holidays to compute
#          the accurate vendor receipt date, skipping weekends and holidays.
#
# Functional Requirements Document (FRD)
# ─────────────────────────────────────
# Background:
#   Standard Odoo adds product.supplierinfo.delay as raw calendar days to
#   purchase.order.date_order. This ignores weekends and public holidays,
#   producing inaccurate planned receipt dates.
#
# Business Rule:
#   Receipt Date = Confirmation Date + Lead Time (working days only)
#
#   Where:
#     - Confirmation Date = purchase.order.date_order
#     - Lead Time         = product.supplierinfo.delay  (working days)
#     - Working days      = days within res.company.resource_calendar_id,
#                           excluding weekends and resource.calendar.leaves
#                           (public holidays)
#
# FR-01 – Line Date:
#   Recompute purchase.order.line.date_planned using
#   resource.calendar.plan_days(delay, date_order, compute_leaves=True).
#
# FR-02 – Order Date:
#   purchase.order.date_planned is computed from the minimum of its lines'
#   date_planned in standard Odoo, so it inherits the correction automatically.
#
# FR-03 – Fallback:
#   If no resource calendar is configured on the company, fall back to
#   adding calendar days (timedelta(days=delay)).
#
# FR-04 – Zero Delay:
#   If delay == 0, return date_order unchanged.
#
# FR-05 – Triggers:
#   Recompute whenever date_order, product_id, product_qty, product_uom,
#   or company_id changes (matching base _compute_date_planned depends).
##############################################################################
{
    "name": "Actual Vendor Lead Time",
    "version": "19.0.1.0.0",
    "summary": (
        "Uses the company's Resource Calendar and Public Holidays to compute "
        "accurate vendor lead times, skipping weekends and holidays."
    ),
    "description": """
Actual Vendor Lead Time
=======================
Standard Odoo calculates the scheduled receipt date (date_planned) by adding
the vendor's lead time (delay) as raw calendar days to the purchase order
confirmation date.

This module overrides that behaviour: it uses the company's Resource Calendar
(res.company → resource_calendar_id) together with configured Public Holidays
(resource.calendar.leaves) to add the lead time as **working days**, so that
weekends and holidays are automatically skipped.

    Receipt Date = Confirmation Date + Lead Time (working days only)

Both purchase.order.line.date_planned and purchase.order.date_planned are
corrected (the order-level field cascades from the line-level computation).
    """,
    "category": "Purchase",
    "author": "NOVOBI",
    "website": "https://www.novobi.com",
    "depends": ["purchase"],
    "data": [],
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "LGPL-3",
}
