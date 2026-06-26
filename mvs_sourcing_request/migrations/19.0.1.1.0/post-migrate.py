# -*- coding: utf-8 -*-
"""MVS-025 — backfill the baseline snapshot for candidate-vendor rows that
predate this feature.

The TDD assumed a greenfield database (no rows to backfill), but the live
deployment already holds MVS-003 candidate-vendor rows. Without a baseline they
would render as "changed" in the Tracking Change tab. Seed base_* from the
current figures for any row whose baseline was never captured. Idempotent
(only touches rows where base_price IS NULL).
"""


def migrate(cr, version):
    cr.execute(
        """
        UPDATE sourcing_request_line_vendor
           SET base_price = price,
               base_delay = delay,
               base_min_qty = min_qty,
               base_payment_term_id = payment_term_id
         WHERE base_price IS NULL
        """
    )
