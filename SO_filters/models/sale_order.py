from odoo import api, fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    is_international = fields.Boolean(
        string="International",
        compute="_compute_is_international",
        store=True,
        search="_search_is_international",
    )

    @api.depends("partner_id.country_id", "company_id.country_id")
    def _compute_is_international(self):
        for order in self:
            partner_country = order.partner_id.country_id
            company_country = order.company_id.country_id
            order.is_international = (
                bool(partner_country)
                and bool(company_country)
                and partner_country != company_country
            )

    def _search_is_international(self, operator, value):
        if (operator == "=" and value) or (operator == "!=" and not value):
            return [
                ("partner_id.country_id", "!=", False),
                ("partner_id.country_id", "!=", self.env.company.country_id.id),
            ]
        return [
            "|",
            ("partner_id.country_id", "=", False),
            ("partner_id.country_id", "=", self.env.company.country_id.id),
        ]
