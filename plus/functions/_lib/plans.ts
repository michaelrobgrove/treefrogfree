/** Tree Frog Plus — plan × bouquet matrix.
 *
 * 4 bouquets, single-country (US-only or Canada-only):
 *   us_wo  = US, no adult
 *   us_w   = US, with adult
 *   ca_wo  = Canada, no adult
 *   ca_w   = Canada, with adult
 *
 * Pricing is per plan length (1, 3, 6, 12 months) and is the
 * SAME for all bouquets. The bouquet just controls which
 * package is provisioned in the Gold Panel.
 *
 *  Plan months   Total   /mo
 *    1            $15    $15
 *    3            $36    $12
 *    6            $60    $10
 *   12            $96    $8
 *
 * Renewals charge the same total (no "month-to-month premium")
 * — the renewal menu is just "buy another 1 / 3 / 6 / 12 month
 * plan".
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

export type PlanMonths = 1 | 3 | 6 | 12;
export type BouquetId = "us_wo" | "us_w" | "ca_wo" | "ca_w";

export const BOUQUET_IDS: BouquetId[] = ["us_wo", "us_w", "ca_wo", "ca_w"];

export const BOUQUET_LABELS: Record<BouquetId, string> = {
    "us_wo": "US",
    "us_w":  "US + Adult",
    "ca_wo": "Canada",
    "ca_w":  "Canada + Adult",
};

/** Per-month rate, used in UI badges like "$12 / month". */
export const PLAN_PER_MONTH: Record<PlanMonths, number> = {
    1:  15,
    3:  12,
    6:  10,
    12:  8,
};

/** Total price, used in checkout/renewal Orders and the
 *  pricing-page hero numbers. */
export const PLAN_PRICES: Record<PlanMonths, number> = {
    1:  15,
    3:  36,
    6:  60,
    12: 96,
};

export function priceFor(months: PlanMonths): number {
    return PLAN_PRICES[months];
}

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

/** Reverse lookup: given a Gold Panel numeric bouquet id,
 *  return our internal BouquetId — or null if it doesn't
 *  match one of the 4 we know about. Returns null for any
 *  custom/unknown bouquet the operator may have created. */
export function panelIdToBouquet(panelId: string | null | undefined): BouquetId | null {
    if (!panelId) return null;
    for (const k of BOUQUET_IDS) {
        if (bouquetToPanelId(k) === String(panelId)) return k;
    }
    return null;
}

/** True if the bouquet key is one of the 4 standard ones
 *  (i.e. self-serve renewal is available). False means the
 *  customer is on a custom panel bouquet and needs to
 *  contact support to renew. */
export function isStandardBouquet(b: BouquetId | null): b is BouquetId {
    return b !== null && (BOUQUET_IDS as string[]).includes(b);
}

/** Display label for any bouquet key, including the synthetic
 *  "custom" sentinel. The dashboard uses this so we can show
 *  "Custom" (with the actual numeric id underneath) for
 *  GP-only customers on non-standard bouquets. */
export function bouquetDisplayLabel(
    bouquet: BouquetId | "custom",
    panelId: string | null | undefined = null,
): string {
    if (bouquet === "custom") {
        return panelId ? `Custom (panel ${panelId})` : "Custom";
    }
    return BOUQUET_LABELS[bouquet] || bouquet;
}
