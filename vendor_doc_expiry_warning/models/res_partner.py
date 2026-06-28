import logging
from datetime import date, timedelta

from markupsafe import Markup, escape

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

_EXPIRY_WARNING_DAYS = 30
_ACTIVITY_DEADLINE_DAYS = 14


class ResPartner(models.Model):
    """Extend res.partner with W-9 / COI document tracking and expiry pre-warning.

    Two cron methods drive the lifecycle:

    ``_cron_vendor_doc_expiry_warning`` (daily)
        - Sets ``is_document_expiring_soon = True`` when a required document
          expires within the next 30 days and creates a "Document Expiration
          Follow-Up" activity assigned to ``followup_responsible_id``.
        - Clears the flag when no document is expiring within 30 days anymore.
        - Activity de-duplication: skips creation if an open activity of the
          same type already exists on the partner.

    ``_cron_vendor_doc_weekly_summary`` (weekly)
        - Sends a consolidated HTML email to all active users in the
          ``purchase.group_purchase_manager`` group, listing vendors whose
          documents are expiring, sorted by nearest expiration date.

    "Vendor manager" is ``followup_responsible_id`` (Many2one res.users), a
    dedicated field added by this module.  It is intentionally separate from
    ``user_id`` (Salesperson) so that document follow-up ownership can differ
    from the commercial contact.
    """

    _inherit = "res.partner"

    w9_needed = fields.Boolean(
        string="W-9 Needed",
        default=False,
        help="Check if this vendor is required to provide a W-9 form.",
    )
    w9_expiration_date = fields.Date(
        string="W-9 Expiration Date",
        help="Date on which the vendor's W-9 document expires.",
        index="btree_not_null",
    )
    coi_needed = fields.Boolean(
        string="COI Needed",
        default=False,
        help="Check if this vendor is required to provide a Certificate of Insurance.",
    )
    coi_expiration_date = fields.Date(
        string="COI Expiration Date",
        help="Date on which the vendor's Certificate of Insurance expires.",
        index="btree_not_null",
    )
    is_document_expiring_soon = fields.Boolean(
        string="Document Expiring Soon",
        default=False,
        readonly=True,
        copy=False,
        help="Automatically set by the daily cron when a required document "
             "expires within the next 30 days.",
    )
    followup_responsible_id = fields.Many2one(
        comodel_name="res.users",
        string="Document Follow-Up Responsible",
        ondelete="set null",
        tracking=True,
        help="Internal user responsible for following up on this vendor's "
             "document renewals. Assigned as the activity owner by the daily cron.",
    )

    # ------------------------------------------------------------------
    # Daily cron: flag vendors with documents expiring within 30 days
    # ------------------------------------------------------------------

    @api.model
    def _cron_vendor_doc_expiry_warning(self):
        """Daily cron — set/clear the pre-expiration flag and schedule activities.

        Selection criteria (set flag):
            supplier_rank > 0
            AND (
                (w9_needed AND today <= w9_expiration_date <= today+30)
                OR
                (coi_needed AND today <= coi_expiration_date <= today+30)
            )

        Clear criteria:
            supplier_rank > 0
            AND is_document_expiring_soon = True
            AND (w9_needed = False OR w9_expiration_date > today+30)
            AND (coi_needed = False OR coi_expiration_date > today+30)
        """
        today = date.today()
        threshold = today + timedelta(days=_EXPIRY_WARNING_DAYS)
        today_str = today.isoformat()
        threshold_str = threshold.isoformat()

        activity_type = self.env.ref(
            "vendor_doc_expiry_warning.activity_type_document_expiration_followup",
            raise_if_not_found=False,
        )
        if not activity_type:
            _logger.error(
                "Vendor Doc Expiry Warning: activity type XML ID "
                "'vendor_doc_expiry_warning.activity_type_document_expiration_followup' "
                "not found — activities will not be created."
            )

        expiring_vendors = self.sudo().search([
            ("supplier_rank", ">", 0),
            "|",
            "&", ("w9_needed", "=", True),
                 "&", ("w9_expiration_date", ">=", today_str),
                      ("w9_expiration_date", "<=", threshold_str),
            "&", ("coi_needed", "=", True),
                 "&", ("coi_expiration_date", ">=", today_str),
                      ("coi_expiration_date", "<=", threshold_str),
        ])

        _logger.info(
            "Vendor Doc Expiry Warning: found %d vendor(s) with documents expiring within %d days.",
            len(expiring_vendors),
            _EXPIRY_WARNING_DAYS,
        )

        for vendor in expiring_vendors:
            if not vendor.is_document_expiring_soon:
                vendor.is_document_expiring_soon = True

            if activity_type:
                self._schedule_expiry_activity(vendor, activity_type, today)

        # Clear flag for vendors whose documents are no longer within the warning window
        vendors_to_clear = self.sudo().search([
            ("supplier_rank", ">", 0),
            ("is_document_expiring_soon", "=", True),
            "&",
            "|",
            ("w9_needed", "=", False),
            ("w9_expiration_date", ">", threshold_str),
            "|",
            ("coi_needed", "=", False),
            ("coi_expiration_date", ">", threshold_str),
        ])

        if vendors_to_clear:
            vendors_to_clear.write({"is_document_expiring_soon": False})
            _logger.info(
                "Vendor Doc Expiry Warning: cleared pre-expiration flag for %d vendor(s).",
                len(vendors_to_clear),
            )

    def _schedule_expiry_activity(self, vendor, activity_type, today):
        """Create a follow-up activity on the vendor if none already exists.

        Skips creation when:
        - ``followup_responsible_id`` is not set on the vendor.
        - An open activity of the same type already exists on the partner.

        :param vendor: ``res.partner`` record (single)
        :param activity_type: ``mail.activity.type`` record
        :param today: :class:`datetime.date` — current date
        """
        if not vendor.followup_responsible_id:
            _logger.warning(
                "Vendor Doc Expiry Warning: vendor '%s' (id=%s) has no "
                "Document Follow-Up Responsible — activity skipped.",
                vendor.name,
                vendor.id,
            )
            return

        existing_activity = self.env["mail.activity"].sudo().search(
            [
                ("res_model", "=", "res.partner"),
                ("res_id", "=", vendor.id),
                ("activity_type_id", "=", activity_type.id),
            ],
            limit=1,
        )
        if existing_activity:
            return

        expiry_lines = []
        if vendor.w9_needed and vendor.w9_expiration_date:
            expiry_lines.append(
                f"<li>W-9 expires on <b>{vendor.w9_expiration_date}</b></li>"
            )
        if vendor.coi_needed and vendor.coi_expiration_date:
            expiry_lines.append(
                f"<li>COI expires on <b>{vendor.coi_expiration_date}</b></li>"
            )

        note = (
            f"<p>The following documents for <b>{vendor.name}</b> are expiring "
            f"within {_EXPIRY_WARNING_DAYS} days:</p>"
            f"<ul>{''.join(expiry_lines)}</ul>"
            f"<p>Please follow up to ensure renewal before the expiration date.</p>"
        )

        deadline = today + timedelta(days=_ACTIVITY_DEADLINE_DAYS)
        vendor.activity_schedule(
            act_type_xmlid=(
                "vendor_doc_expiry_warning.activity_type_document_expiration_followup"
            ),
            date_deadline=deadline,
            summary=f"Document expiring soon — {vendor.name}",
            note=note,
            user_id=vendor.followup_responsible_id.id,
        )
        _logger.info(
            "Vendor Doc Expiry Warning: scheduled activity for vendor '%s' (id=%s), "
            "assigned to '%s', deadline %s.",
            vendor.name,
            vendor.id,
            vendor.followup_responsible_id.name,
            deadline,
        )

    # ------------------------------------------------------------------
    # Weekly cron: summary email to purchase managers
    # ------------------------------------------------------------------

    @api.model
    def _cron_vendor_doc_weekly_summary(self):
        """Weekly cron — email a summary of expiring vendor documents to purchase managers.

        Recipients: all active internal users in ``purchase.group_purchase_manager``.

        The summary is sorted by the nearest expiration date across both
        W-9 and COI columns.
        """
        today = date.today()
        threshold = today + timedelta(days=_EXPIRY_WARNING_DAYS)
        today_str = today.isoformat()
        threshold_str = threshold.isoformat()

        expiring_vendors = self.sudo().search([
            ("supplier_rank", ">", 0),
            "|",
            "&", ("w9_needed", "=", True),
                 "&", ("w9_expiration_date", ">=", today_str),
                      ("w9_expiration_date", "<=", threshold_str),
            "&", ("coi_needed", "=", True),
                 "&", ("coi_expiration_date", ">=", today_str),
                      ("coi_expiration_date", "<=", threshold_str),
        ])

        if not expiring_vendors:
            _logger.info(
                "Vendor Doc Expiry Weekly Summary: no vendors with expiring "
                "documents — no email sent."
            )
            return

        purchase_manager_group = self.env.ref(
            "purchase.group_purchase_manager", raise_if_not_found=False
        )
        if not purchase_manager_group:
            _logger.error(
                "Vendor Doc Expiry Weekly Summary: group "
                "'purchase.group_purchase_manager' not found — email skipped."
            )
            return

        recipients = self.env["res.users"].sudo().search([
            ("active", "=", True),
            ("share", "=", False),
            ("group_ids", "in", [purchase_manager_group.id]),
        ])
        if not recipients:
            _logger.warning(
                "Vendor Doc Expiry Weekly Summary: no active internal users in "
                "purchase.group_purchase_manager — email skipped."
            )
            return

        sorted_vendors = sorted(
            expiring_vendors,
            key=lambda v: min(
                filter(
                    None,
                    [
                        v.w9_expiration_date if v.w9_needed else None,
                        v.coi_expiration_date if v.coi_needed else None,
                    ],
                ),
                default=date.max,
            ),
        )

        template = self.env.ref(
            "vendor_doc_expiry_warning.email_template_vendor_expiry_summary",
            raise_if_not_found=False,
        )
        if not template:
            _logger.error(
                "Vendor Doc Expiry Weekly Summary: email template "
                "'vendor_doc_expiry_warning.email_template_vendor_expiry_summary' "
                "not found — email skipped."
            )
            return

        rows = ""
        for vendor in sorted_vendors:
            w9_date = vendor.w9_expiration_date if vendor.w9_needed else "N/A"
            coi_date = vendor.coi_expiration_date if vendor.coi_needed else "N/A"
            responsible = (
                vendor.followup_responsible_id.name
                if vendor.followup_responsible_id
                else "—"
            )
            rows += (
                f"<tr>"
                f"<td style='padding:4px 8px;border:1px solid #ccc'>{escape(vendor.name)}</td>"
                f"<td style='padding:4px 8px;border:1px solid #ccc'>{w9_date}</td>"
                f"<td style='padding:4px 8px;border:1px solid #ccc'>{coi_date}</td>"
                f"<td style='padding:4px 8px;border:1px solid #ccc'>{escape(responsible)}</td>"
                f"</tr>"
            )

        expiry_table = (
            f"<table style='border-collapse:collapse;font-size:14px'>"
            f"<thead><tr>"
            f"<th style='padding:4px 8px;border:1px solid #ccc;background:#f0f0f0'>Vendor</th>"
            f"<th style='padding:4px 8px;border:1px solid #ccc;background:#f0f0f0'>W-9 Expiration</th>"
            f"<th style='padding:4px 8px;border:1px solid #ccc;background:#f0f0f0'>COI Expiration</th>"
            f"<th style='padding:4px 8px;border:1px solid #ccc;background:#f0f0f0'>Follow-Up Responsible</th>"
            f"</tr></thead>"
            f"<tbody>{rows}</tbody>"
            f"</table>"
        )

        company = self.env.company
        rendered_body = template._render_field(
            "body_html",
            [company.id],
            add_context={"expiry_table": Markup(expiry_table)},
        )[company.id]
        rendered_subject = template._render_field(
            "subject",
            [company.id],
        )[company.id]

        recipient_emails = ", ".join(u.email for u in recipients if u.email)
        if not recipient_emails:
            _logger.warning(
                "Vendor Doc Expiry Weekly Summary: no recipient email addresses found — "
                "email skipped."
            )
            return

        mail = self.env["mail.mail"].sudo().create({
            "subject": rendered_subject,
            "email_to": recipient_emails,
            "body_html": rendered_body,
            "auto_delete": False,
        })
        mail.send()
        _logger.info(
            "Vendor Doc Expiry Weekly Summary: sent to %d recipient(s) covering "
            "%d vendor(s).",
            len(recipients),
            len(expiring_vendors),
        )
