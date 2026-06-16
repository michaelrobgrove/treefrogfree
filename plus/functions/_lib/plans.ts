/** Tree Frog Plus — plan × bouquet matrix.
 *
 * 12 SKUs total: 3 plan lengths × 4 bouquets. The PayPal plan IDs
 * are read from env vars (set via `wrangler secret put`); the
 * Gold Panel bouquet IDs are also env vars (queried once via
 * `action=bouquet&api_key=...` after the user creates them in the
 * panel, then baked in via `wrangler pages secret put` or
 * `[vars]` in `plus/wrangler.toml`).
 *
 * The webhook handler uses `paypalPlanToSku()` to map a PayPal
 * subscription's `plan_id` back to the (plan_months, bouquet) pair
 * the customer bought — so the function doesn't have to re-derive
 * it from the price.
 *
 * If a SKU is misconfigured (env var missing), the function falls
 * back to a clear error rather than guessing.
 */

export type PlanMonths = 3 | 6 | 12;
export type BouquetId = "us" | "us_adult" | "us_ca" | "us_ca_adult";

export const BOUQUET_IDS: BouquetId[] = ["us", "us_adult", "us_ca", "us_ca_adult"];

export const BOUQUET_LABELS: Record<BouquetId, string> = {
    "us":          "US",
    "us_adult":    "US + Adult",
    "us_ca":       "US & Canada",
    "us_ca_adult": "US & Canada + Adult",
};

export const PLAN_PRICES: Record<PlanMonths, number> = {
    3:  36,
    6:  60,
    12: 120,
};

/** Look up the PayPal plan ID for a given (months, bouquet) SKU.
 *  Reads the env var and throws a clear error if it's missing. */
export function skuToPaypalPlan(months: PlanMonths, bouquet: BouquetId): string {
    const key = `PAYPAL_PLAN_${months}M_${bouquet.toUpperCase()}`;
    const v = (globalThis as any)[key];
    if (typeof v !== "string" || !v) {
        throw new Error(
            `PayPal plan not configured for ${months}mo / ${bouquet}. ` +
            `Set env var ${key}.`
        );
    }
    return v;
}

/** Map a PayPal plan_id back to the (months, bouquet) pair.
 *  Returns null if the plan_id isn't one of ours. */
export function paypalPlanToSku(planId: string): { months: PlanMonths; bouquet: BouquetId } | null {
    for (const m of [3, 6, 12] as PlanMonths[]) {
        for (const b of BOUQUET_IDS) {
            try {
                if (skuToPaypalPlan(m, b) === planId) return { months: m, bouquet: b };
            } catch (e) { /* env var missing — skip */ }
        }
    }
    return null;
}

/** Map our bouquet id to the Gold Panel numeric bouquet id. */
export function bouquetToPanelId(bouquet: BouquetId): string {
    const key = `BOUQUET_${bouquet.toUpperCase()}`;
    const v = (globalThis as any)[key];
    if (typeof v !== "string" || !v) {
        throw new Error(
            `Gold Panel bouquet not configured for ${bouquet}. ` +
            `Set env var ${key} to the numeric id from ` +
            `action=bouquet&api_key=...`
        );
    }
    return v;
}
