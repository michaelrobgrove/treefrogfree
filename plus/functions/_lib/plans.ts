/** Tree Frog Plus — plan × bouquet matrix.
 *
 * 4 bouquets, single-country (US-only or Canada-only):
 *   us_wo  = US, no adult
 *   us_w   = US, with adult
 *   ca_wo  = Canada, no adult
 *   ca_w   = Canada, with adult
 *
 * Pricing is per plan length (3, 6, 12 months) and is the SAME
 * for all bouquets. The bouquet just controls which package
 * is provisioned in the Gold Panel.
 *
 * Gold Panel bouquet IDs come from the `action=bouquet`
 * response. They're baked in via env vars
 * (`BOUQUET_US_WO`, `BOUQUET_US_W`, `BOUQUET_CA_WO`,
 * `BOUQUET_CA_W`). Defaults below match what the operator
 * confirmed in the Gold Panel:
 *   us_wo → 66496
 *   us_w  → 67000
 *   ca_wo → 67006
 *   ca_w  → 67007
 *
 * We use PayPal Orders (one-time payments) for billing, not
 * PayPal Subscriptions — the site generates a payment link
 * at renewal time. So there are no `PAYPAL_PLAN_*` env vars.
 * The price is taken from PLAN_PRICES below.
 */

export type PlanMonths = 3 | 6 | 12;
export type BouquetId = "us_wo" | "us_w" | "ca_wo" | "ca_w";

export const BOUQUET_IDS: BouquetId[] = ["us_wo", "us_w", "ca_wo", "ca_w"];

export const BOUQUET_LABELS: Record<BouquetId, string> = {
    "us_wo": "US",
    "us_w":  "US + Adult",
    "ca_wo": "Canada",
    "ca_w":  "Canada + Adult",
};

/** Map the user's bouquet selection to the Gold Panel numeric
 *  bouquet id. Hard-coded defaults match the operator's panel;
 *  override with the BOUQUET_* env vars if the panel changes. */
export function bouquetToPanelId(bouquet: BouquetId): string {
    const envKey = `BOUQUET_${bouquet.toUpperCase()}`;
    const envVal = (globalThis as any)[envKey];
    if (typeof envVal === "string" && envVal) return envVal;
    // Hard-coded fallbacks from the operator's Gold Panel.
    const fallback: Record<BouquetId, string> = {
        "us_wo": "66496",
        "us_w":  "67000",
        "ca_wo": "67006",
        "ca_w":  "67007",
    };
    return fallback[bouquet];
}

export const PLAN_PRICES: Record<PlanMonths, number> = {
    3:  36,
    6:  60,
    12: 120,
};

export function priceFor(months: PlanMonths): number {
    return PLAN_PRICES[months];
}

/** Renewal months — includes +1 (we let customers top up
 *  one month at a time even though the initial signups are
 *  always 3+). The price is $12/month flat, matching the
 *  per-month rate of the 3-month plan. */
export type RenewalMonths = 1 | 3 | 6 | 12;

export const RENEWAL_PRICE_PER_MONTH = 12;

export function priceForRenewal(months: RenewalMonths): number {
    return months * RENEWAL_PRICE_PER_MONTH;
}
