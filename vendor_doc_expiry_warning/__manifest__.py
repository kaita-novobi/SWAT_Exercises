{
    "name": "Vendor Document Expiration Pre-Warning",
    "version": "19.0.1.0.0",
    "summary": (
        "30-day advance warning for expiring vendor W-9 and COI documents; "
        "sets a pre-expiration flag, creates follow-up activities, and sends "
        "a weekly summary to purchase managers."
    ),
    "category": "Purchase",
    "author": "NOVOBI",
    "depends": ["base", "mail", "purchase"],
    "data": [
        "data/activity_type_data.xml",
        "data/ir_cron_data.xml",
        "views/res_partner_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "LGPL-3",
}
