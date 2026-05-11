{
    "name": "SO Without Delivery Method Wizard",
    "version": "19.0.1.0.0",
    "summary": "Shows a popup wizard when confirming a Sales Order that has no delivery method set.",
    "category": "Sales",
    "author": "NOVOBI",
    "depends": ["sale", "delivery"],
    "data": [
        "security/ir.model.access.csv",
        "views/sale_delivery_confirm_wizard_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "LGPL-3",
}
