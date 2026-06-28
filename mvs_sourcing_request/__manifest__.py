# -*- coding: utf-8 -*-
{
    "name": "Sourcing Request & Vendor Comparison",
    "version": "19.0.1.1.0",
    "summary": (
        "Multi-vendor sourcing for sales orders: raise a sourcing request, "
        "route lines automatically or through an assisted RFQ negotiation, "
        "compare candidate vendors, and confirm the winning RFQs into POs."
    ),
    "category": "Purchase",
    "author": "NOVOBI",
    "website": "https://www.novobi.com",
    "depends": [
        "sale",
        "purchase",
        "purchase_stock",
        "stock",
        "product",
        "mail",
    ],
    "data": [
        "security/ir.model.access.csv",
        "security/sourcing_request_security.xml",
        "data/ir_sequence_data.xml",
        "data/ir_config_parameter_data.xml",
        "views/sourcing_request_views.xml",
        "views/sale_order_views.xml",
        "views/purchase_order_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "mvs_sourcing_request/static/src/vendor_matrix/vendor_matrix.scss",
            "mvs_sourcing_request/static/src/vendor_matrix/vendor_matrix.xml",
            "mvs_sourcing_request/static/src/vendor_matrix/vendor_matrix.js",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "LGPL-3",
}
