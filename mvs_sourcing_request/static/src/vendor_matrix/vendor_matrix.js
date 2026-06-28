/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onWillStart, useState } from "@odoo/owl";

/**
 * Vendor Comparison matrix for the Sourcing Request form.
 *
 * Bound to the computed `all_vendor_line_ids` One2many: it reads the candidate
 * vendor rows from the field datapoint, groups them client-side (by product or by
 * vendor), and renders the mockup layout with per-cell best-price / fastest-lead
 * highlights. Inline edits persist directly via `orm.write` on the
 * `sourcing.request.line.vendor` model, then reload the form so the server-side
 * computes (is_best_*, totals) and the chatter logging refresh — no business logic
 * is duplicated in JS.
 */
export class VendorMatrixField extends Component {
    static template = "mvs_sourcing_request.VendorMatrix";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({ groupBy: "product", paymentTerms: [] });

        onWillStart(async () => {
            this.state.paymentTerms = await this.orm.searchRead(
                "account.payment.term",
                [],
                ["id", "display_name"],
            );
        });
    }

    // ------------------------------------------------------------------
    // Data access
    // ------------------------------------------------------------------
    get list() {
        return this.props.record.data[this.props.name];
    }

    get records() {
        return this.list.records || [];
    }

    /** Many2one helpers — datapoint stores m2o as [id, displayName]. */
    m2oId(value) {
        return Array.isArray(value) ? value[0] : value && value.id;
    }

    m2oName(value) {
        return Array.isArray(value) ? value[1] : (value && value.display_name) || "";
    }

    /**
     * Bucket the vendor rows by product or by vendor.
     * Returns: [{ key, title, meta, records: [Record] }]
     */
    get groups() {
        const byProduct = this.state.groupBy === "product";
        const buckets = new Map();
        for (const rec of this.records) {
            const data = rec.data;
            const keyVal = byProduct ? data.product_id : data.partner_id;
            const key = this.m2oId(keyVal);
            if (!buckets.has(key)) {
                buckets.set(key, {
                    key,
                    title: this.m2oName(keyVal),
                    qty: byProduct ? data.product_qty : 0,
                    records: [],
                });
            }
            buckets.get(key).records.push(rec);
        }
        return [...buckets.values()].map((g) => {
            const count = g.records.length;
            const meta = byProduct
                ? `required ${this.formatQty(g.qty)} Units · ${count} candidate vendor(s)`
                : `${count} product line(s)`;
            return { ...g, meta };
        });
    }

    get isGroupByProduct() {
        return this.state.groupBy === "product";
    }

    formatQty(value) {
        const n = Number(value || 0);
        return Number.isInteger(n) ? String(n) : n.toFixed(2);
    }

    // ------------------------------------------------------------------
    // Actions
    // ------------------------------------------------------------------
    setGroupBy(mode) {
        this.state.groupBy = mode;
    }

    /** Persist one cell edit, then reload the form to refresh server computes. */
    async commitCell(rec, field, rawValue, type) {
        let value = rawValue;
        if (type === "float" || type === "monetary") {
            value = parseFloat(rawValue);
            if (isNaN(value)) {
                return;
            }
        } else if (type === "integer") {
            value = parseInt(rawValue, 10);
            if (isNaN(value)) {
                return;
            }
        } else if (type === "many2one") {
            value = rawValue ? parseInt(rawValue, 10) : false;
        }
        // No-op guard: skip a write when the value did not actually change.
        const current =
            type === "many2one" ? this.m2oId(rec.data[field]) : rec.data[field];
        if (current === value) {
            return;
        }
        try {
            await this.orm.write("sourcing.request.line.vendor", [rec.resId], {
                [field]: value,
            });
            await this.props.record.load();
        } catch (error) {
            // Surface validation errors (e.g. BR-005 qty range) and revert by reloading.
            await this.props.record.load();
            throw error;
        }
    }

    onNumberChange(rec, field, ev, type) {
        return this.commitCell(rec, field, ev.target.value, type);
    }

    onTextChange(rec, field, ev) {
        return this.commitCell(rec, field, ev.target.value, "char");
    }

    onTermChange(rec, ev) {
        return this.commitCell(rec, "payment_term_id", ev.target.value, "many2one");
    }

    async onSelectedToggle(rec, ev) {
        await this.commitCell(rec, "selected", ev.target.checked, "boolean");
    }

    openRfq(rec) {
        const id = this.m2oId(rec.data.rfq_id);
        if (!id) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "purchase.order",
            res_id: id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    rfqLabel(rec) {
        return this.m2oName(rec.data.rfq_id);
    }
}

export const vendorMatrixField = {
    component: VendorMatrixField,
    supportedTypes: ["one2many"],
};

registry.category("fields").add("sourcing_vendor_matrix", vendorMatrixField);
