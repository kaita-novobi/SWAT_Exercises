# -*- coding: utf-8 -*-
"""Move the candidate-vendor link from the RFQ header down to the RFQ line.

RFQs are now merged by vendor (one RFQ → many candidate lines), so the link to
the ``sourcing.request.line.vendor`` row lives on ``purchase.order.line`` instead
of ``purchase.order``. Existing single-product candidate RFQs created under the
old 1:1 model carry the link on the header column, which the ORM will drop when
the field is removed during this upgrade. Copy it onto each order line first so
the forward/back sync keeps working after the upgrade.

Runs in pre-migrate while the old ``purchase_order.sourcing_vendor_line_id``
column still exists; the new line column is created up-front here (IF NOT EXISTS)
and then formally registered by the ORM during module load. Idempotent.
"""


def migrate(cr, version):
    # Old header column may already be gone on a re-run — guard on its presence.
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'purchase_order'
           AND column_name = 'sourcing_vendor_line_id'
        """
    )
    if not cr.fetchone():
        return

    cr.execute(
        """
        ALTER TABLE purchase_order_line
          ADD COLUMN IF NOT EXISTS sourcing_vendor_line_id integer
        """
    )
    # Each legacy candidate RFQ has exactly one product line; copy the header link
    # onto that line. Only fill lines not already linked (idempotent).
    cr.execute(
        """
        UPDATE purchase_order_line pol
           SET sourcing_vendor_line_id = po.sourcing_vendor_line_id
          FROM purchase_order po
         WHERE pol.order_id = po.id
           AND po.sourcing_vendor_line_id IS NOT NULL
           AND pol.sourcing_vendor_line_id IS NULL
        """
    )
