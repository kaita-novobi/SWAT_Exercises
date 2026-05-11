import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    """Extends sale.order to provide the daily pending quotation reminder cron logic."""

    _inherit = "sale.order"

    # ---------------------------------------------------------------------------
    # Cron Methods
    # ---------------------------------------------------------------------------

    @api.model
    def _cron_send_pending_quotation_reminder(self):
        """Send one consolidated reminder email per salesperson for all their pending quotations.

        Logic:
        - Find all sale.order records with state 'draft' or 'sent'.
        - Group them by user_id (salesperson).
        - For each salesperson that has a valid email address, compose and dispatch
          a single HTML email listing every pending SO reference.

        This method is invoked by the 'Daily Pending Quotations' ir.cron record.
        """
        # Step 1: Retrieve all draft/sent quotations across all companies.
        pending_orders = self.sudo().search([("state", "in", ["draft", "sent"])])

        if not pending_orders:
            _logger.info("Daily Pending Quotations: no pending quotations found — no emails sent.")
            return

        # Step 2: Group quotations by salesperson (user_id).
        orders_by_user = {}
        for order in pending_orders:
            if not order.user_id:
                continue
            if not order.user_id.email:
                _logger.warning(
                    "Daily Pending Quotations: salesperson '%s' (id=%s) has no email — skipped.",
                    order.user_id.name,
                    order.user_id.id,
                )
                continue
            orders_by_user.setdefault(order.user_id, []).append(order)

        if not orders_by_user:
            _logger.info("Daily Pending Quotations: no salespersons with a valid email found — no emails sent.")
            return

        # Step 3: Send one consolidated email per salesperson.
        MailMail = self.env["mail.mail"].sudo()

        for salesperson, orders in orders_by_user.items():
            so_items = "".join(f"<li>{order.name}</li>" for order in orders)
            body_html = (
                f"<p>Hi {salesperson.name},</p>"
                f"<p>There are quotations that are in pending till today:</p>"
                f"<ul>{so_items}</ul>"
                f"<p>Please take a look at these!</p>"
            )

            try:
                mail = MailMail.create({
                    "subject": "Quotations at the end of each day",
                    "email_to": salesperson.email,
                    "body_html": body_html,
                    "auto_delete": True,
                })
                mail.send()
                _logger.info(
                    "Daily Pending Quotations: sent reminder to '%s' (%s) for %d quotation(s).",
                    salesperson.name,
                    salesperson.email,
                    len(orders),
                )
            except Exception:
                _logger.exception(
                    "Daily Pending Quotations: failed to send reminder to '%s' (%s).",
                    salesperson.name,
                    salesperson.email,
                )
