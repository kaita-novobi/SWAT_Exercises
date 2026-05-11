{
    "name": "Send Emails for Quotations at the End of Each Day",
    "version": "19.0.1.0.0",
    "summary": "Automatically sends daily reminder emails to salespersons for pending (draft/sent) quotations.",
    "category": "Sales",
    "author": "NOVOBI",
    "depends": ["sale"],
    "data": [
        "data/mail_template_data.xml",
        "data/ir_cron_data.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "LGPL-3",
}
