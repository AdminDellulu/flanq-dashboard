# Flanq Dashboard KPI Audit — 2026-05-11

> **STATUS as of 2026-05-11 (later same day): all 11 fixes from §7 shipped.** Detail in §9 below. Two additional caveats SN must know:
> - **Shipping accuracy = ITL (55% of shipments) + Velocity (45% of shipments) combined.** The Velocity backfill earlier today only enriched the velocity rows; ITL rows still have stale shipping/RTO data. Until ITL data is also enriched (via `shipping-sync-itl` or an ITL CSV export), any "shipping" KPI on the dashboard is Velocity-accurate but ITL-incomplete.
> - **The "31% RTO" figure quoted earlier was Velocity-window-only.** The portfolio number (ITL + Velocity combined) is currently understated because ITL's RTOs aren't fully tagged. Will re-run when ITL data is in.


Scope: every KPI in `index.html` + every formula in the 4 Supabase RPCs (`compute_daily_metrics_ist`, `compute_unit_economics`, `get_hourly_metrics`, `get_go_fetch_report`). Cross-checked against the live `flanq-finance` schema and post-Velocity-backfill data.

---

## TL;DR — what the dashboard is currently lying about

| # | KPI | What dashboard shows | What's true | Why | Severity |
|---|---|---|---|---|---|
| 1 | **Today's Net Profit / Margin** | `revenue − cogs − ad_spend` (shipping=0, RTO=0) | Real margin needs 7-30 days to settle | Every cost except cogs+adspend is anchored to events that happen days later, but the date filter pulls from `daily_metrics.order_date` | **Critical** |
| 2 | **Shipping Health card** (Delivery Rate, Avg Time, Delivered, RTO, NDR) | All-time aggregates, ignores the duration selector | Should respond to date filter | `renderOverview` reads `DATA.shipments` directly without filtering by `dur` | **Critical** |
| 3 | **COD Cash Position** (collected, pending) | `DATA.shipments.cod.remitted/locked` are all-time | Should reflect filter window | Same root cause as #2 | **Critical** |
| 4 | **RTO Rate** | Three different formulas across the codebase, all give different numbers | One canonical number | Definition divergence — see §3 | High |
| 5 | **DATA.orders.codOrders / prepaidOrders** | Always `0` | Should equal real counts | `payment_type === 'COD'` filter is case-sensitive but parser stores `'cod'` lowercase | High |
| 6 | **`renderOverview` `prepaidPct`** | `totalPrepaid / totalOrders × 100` | Dimension mismatch | `totalPrepaid` is **count of prepaid orders** (✓), but elsewhere `totalCOD/totalPrepaid` are used as if they were **revenue** (line 2850, 2852, 4610) | High |
| 7 | **Net Profit** in margin formula | `rev − cogs − gst − pkg − ship − gw − ads − rtoCost` | Subtracts `shipping_cost` once and again inside `rtoCost` for RTO shipments | Forward leg counted twice. Margin understated by ~₹110 × (RTO count) per period | High |

Every entry above is fixable; corrections are listed in §6.

---

## 1. Live evidence the date filter is broken

Pulled at 2026-05-11. "Today" = 2026-05-11 IST.

```
orders placed today: 56
shipments billed for those 56 orders today: 0
deliveries against those 56 orders today: 0  ← obviously, they were placed hours ago
COD remit for today's orders: 0  ← COD remit lags ~14 days

But ACTUAL events today:
- shipped: 0
- delivered: 2  (older orders)
- COD remit: 0

Last 7 days:
- orders: 781
- delivered against those orders: 3  (other 778 still in transit)
- delivered events in 7d window: 9  (mostly against orders 7-15 days old)
```

**Implication for "Today" view of the dashboard:**
- Revenue = ✓ (₹X)
- COGS = stale 0 until cron `compute_daily_metrics_ist` runs (then real)
- Shipping cost = **0** (no shipping events have happened yet for today's orders)
- RTO cost = **0** (orders haven't had time to RTO)
- Net Profit = revenue − cogs − ad_spend ← **looks much better than it actually will be once shipments settle**
- Margin % shows as healthy when reality is unknown for ~14 days

This is the single biggest source of the user perception that "KPIs aren't following the right logic."

---

## 2. The Date-Anchor Table

Every KPI must filter on the date column most appropriate for *its semantics*. The dashboard currently anchors everything to `order_date` via `daily_metrics`. Here's the correct mapping:

| KPI | Correct date anchor | Currently uses | Status |
|---|---|---|---|
| Orders, Gross Revenue, Net Revenue, AOV, Discount | `orders.order_date` | order_date | OK |
| Ad Spend, ROAS, CPC, CTR, CAC | `ad_spend.date` | ad_spend.date | OK |
| COGS, GST cost, Packaging | `orders.order_date` (cost realised at order time) | order_date | OK |
| Payment Gateway Fee | `orders.order_date` (charged at capture, ~order time for prepaid) | order_date | OK |
| **Shipping Cost** | `shipments.shipped_at` (Velocity bills at pickup) | order_date | **Wrong** |
| **RTO Cost** | `shipments.rto_initiated_at` | included in order_date | **Wrong** |
| **Delivered Count, Delivery Rate, Avg Delivery Time** | `shipments.delivered_at` | unfiltered (all-time) | **Wrong** |
| **RTO Count, RTO Rate** | `shipments.rto_initiated_at` (or `rto_delivered_at` for closed-loop) | unfiltered | **Wrong** |
| **NDR Count** | `shipments.last_tracking_update` (NDR happens during transit) | unfiltered | **Wrong** |
| **COD Collected (cash in)** | `shipments.cod_remittance_date` (when UTR settles) | order_date | **Wrong** |
| **COD Pending** | derived: orders billed COD whose remit date is null OR in future | derived from order_date proportion | **Wrong** |
| **Settlements (Razorpay)** | `settlements.settled_at` | unfiltered | **Wrong** |
| **Net Profit, Net Cash Position, Margin %** | requires *cohort* logic — pick orders whose full lifecycle has resolved | mixed | **Wrong** |

The "Net Profit" KPI is structurally hard. Two acceptable definitions:
- **(A) Order-cohort margin**: net profit attributable to orders placed in the window, projected with current cost averages. Useful for marketing decisions ("did this campaign make money?"). Requires waiting ~14 days or imputing.
- **(B) Cash-period P&L**: revenue (orders today) minus *costs incurred today regardless of which order they pertain to*. Useful for finance. Each line item filtered by its own anchor date.

Pick one definition per chart and label it. The current dashboard mixes them and labels neither.

---

## 3. Definition Divergences (same KPI, different number)

### 3a. RTO Rate — three definitions in this codebase

| Location | Formula | Result |
|---|---|---|
| `compute_daily_metrics_ist` line ~78 | `rto / (delivered + rto) × 100` | True post-resolution RTO rate (industry-standard) |
| `get_go_fetch_report` line ~38 | `rto_count / total_orders × 100` | Today's RTOs over today's *orders placed* — meaningless |
| `index.html` line 2644 (renderOverview) | `sum(rto)/totalOrders × 100` | Same meaningless definition as the RPC |
| `index.html` line 2773 (Shipping Health) | `rtoCount / totalShipped × 100` | Shipped-base RTO rate |
| `index.html` line 5150 (renderShipping) | `rtoShipCount / totalFiltered × 100` | Same as above but on filtered shipments |

**Canonical fix:** RTO Rate = `RTO / (Delivered + RTO)` — i.e. the fraction of *resolved* shipments that came back. Industry standard. Use this everywhere.

### 3b. `payment_type` case-sensitivity

| Location | Filter | Result |
|---|---|---|
| `compute_daily_metrics_ist` | `LOWER(o.payment_type) = 'cod'` | OK |
| `get_go_fetch_report` | `o.payment_type = 'cod'` (no LOWER) | breaks if any row stored `'COD'` |
| `index.html` line 2397-98 | `o.payment_type === 'COD'` | **always returns 0** because parser writes `'cod'` |

**Canonical fix:** the webhook-shopify parser writes lowercase `'cod'` / `'prepaid'`. Every consumer must use lowercase. Add a CHECK constraint to the `orders.payment_type` column to enforce this.

### 3c. Cancelled/Voided/Refunded filter sets

| Location | Excludes |
|---|---|
| `compute_daily_metrics_ist` | `('cancelled','voided')` |
| `validate_daily_metrics_internal` | `('refunded','voided')` (NOT cancelled, but ADDS refunded) |
| `index.html` line 2226 | only `'voided'` |
| `index.html` line 2243 (perfMap) | `voided`, `refunded`, `partially_refunded`, `restocked` |

These all give different revenue/order counts. **Canonical fix:** establish a single `orders.is_negative` generated column or a view `orders_active` that excludes the agreed-upon set, and have everything read from it.

### 3d. RTO shipment definition

| Location | RTO criteria |
|---|---|
| `index.html` line 2228 | `is_rto OR status IN (rto, rto_in_transit, rto_delivered)` |
| `index.html` line 2275 | `is_rto OR status === 'rto'` (misses in-transit and delivered RTO) |
| `index.html` line 5142 | `_stCount('rto')` (status only, misses is_rto flag) |

**Canonical fix:** single helper `_isRto(s) = s.is_rto === true || ['rto','rto_in_transit','rto_delivered'].includes(s.status)`. Use everywhere.

### 3e. COGS unit convention

`order_items.cogs` is **per-unit**. Every SQL aggregation correctly multiplies by `quantity`:
- `compute_daily_metrics_ist`: `SUM(oi.cogs * oi.quantity)`
- `get_hourly_metrics`: same
- `get_go_fetch_report`: same
- `validate_daily_metrics_internal`: same
- `index.html` line 2256 (perfMap totalCogs): `cogs * quantity`

But **my webhook-shopify v5 wrote per-line cogs** (`cogs = unit_cost * quantity`) which would have caused `SUM(oi.cogs * oi.quantity)` to square the quantity for any line with qty > 1 inserted between v5 deploy and the next `refresh_order_items_cogs` cron run. Reverted to per-unit in **v6 (deployed 2026-05-11)** + ran `refresh_order_items_cogs` to re-normalize the 19,317 existing rows. State is now consistent. See §7.

---

## 4. KPI Formula Errors (correct data, wrong math)

### 4a. RTO cost is double-subtracted from margin (`compute_daily_metrics_ist`)

```sql
contribution_margin = gross_revenue
                    - total_cogs - total_gst - total_packaging
                    - total_shipping        -- includes RTO shipments' forward leg
                    - total_gateway_fees
                    - total_ad_spend
                    - total_rto_cost        -- shipping_cost + rto_cost for RTO ships
```

`total_shipping` sums `shipping_cost` over ALL shipments (delivered AND RTO). `total_rto_cost` then adds `shipping_cost + rto_cost` again for RTO shipments. Forward leg counted twice. Margin understated by `(forward leg cost) × (RTO count)` per period.

**Fix:** `total_rto_cost` should only include the RTO leg, not the forward leg:
```sql
COALESCE((SELECT SUM(s.rto_cost) FROM shipments s ... WHERE is_rto = true), 0)
```

### 4b. GST hardcoded at 18% (`compute_daily_metrics_ist`)

```sql
SUM(oi.cogs * oi.quantity * 0.18) AS total_gst_cost
```

Products have `gst_percent` per row (0%, 5%, 12%, 18%, 28% in Indian taxation). Hardcoded 18% misstates GST cost for every non-18% SKU.

**Fix:** use `oi.gst_cost` (already populated by `refresh_order_items_cogs` from `p.gst_percent`):
```sql
SUM(oi.gst_cost * oi.quantity)
```

### 4c. `compute_unit_economics`: COGS shouldn't depend on delivery rate

```sql
ROUND(pd.cost_per_item * (1 + pd.gst_percent/100) * COALESCE(pd.delivery_rate/100, 0.7), 2) as cogs_per_attempt
```

This says "expected COGS per attempt = unit cost × (1+GST) × delivery_rate". That assumes inventory is *consumed* whether or not the order delivers. **For RTO orders the inventory comes back to warehouse** — only the forward shipping leg is sunk, not the unit cost.

**Fix:**
```sql
ROUND(pd.cost_per_item * (1 + pd.gst_percent/100), 2) as cogs_per_attempt
```

(Drop the delivery_rate multiplication. This is the biggest economic error in the unit economics view — it makes RTO-heavy SKUs look ~30% more profitable than they actually are.)

### 4d. `compute_unit_economics`: hardcoded ₹110 default shipping cost

```sql
ROUND(AVG(COALESCE(s.shipping_cost, 110)), 0) as avg_shipping_cost
```

We just got rid of all the hardcoded ₹110 in actual shipments via the Velocity backfill. This RPC is the last place still defaulting to ₹110.

**Fix:** use `AVG(s.shipping_cost) FILTER (WHERE s.shipping_cost > 0)` and have the consumer handle null.

### 4e. CAC vs CPP confusion (`compute_unit_economics`)

`blended_cpp_ex_gst = SUM(spend) / SUM(purchases)` uses Meta's attributed `purchases` field. That's CPP (cost per Meta-attributed purchase), not CAC (total spend / total new customers).

For Flanq's scale, Meta's attribution drift is significant. Two different numbers conflated as one. **Fix:** rename to `blended_cpp` and add a separate `blended_cac = total_spend / actual_new_orders` from Shopify-side data.

### 4f. `renderOverview` `codRevenue` derivation is unnecessarily proportional

```js
const codRevenue = totalCOD > 0 ? totalRev * (totalCOD/(totalCOD+totalPrepaid+0.01)) : 0;
```

`totalCOD` and `totalPrepaid` here are *order counts* from `daily_metrics`. Multiplying total revenue by the count-share assumes COD orders have the same AOV as prepaid. They usually don't (COD AOV is often lower).

**Fix:** sum revenue directly by payment type. Add to `daily_metrics`:
```sql
gross_revenue_cod = SUM(o.total) FILTER (WHERE LOWER(payment_type)='cod' AND ...)
gross_revenue_prepaid = SUM(o.total) FILTER (WHERE LOWER(payment_type) IN ('prepaid','online') AND ...)
```
Then read directly. No proportion math.

### 4g. `aggregateByGranularity` recomputes `margin` correctly but *not* `rtoRate` consistently

Line 2529: `margin = sum(profit)/sum(rev) * 100` — weighted, correct.
Line 2534: `rtoRate = sum(rto)/sum(orders) * 100` — uses orders denominator, *but* the underlying `daily_metrics.rto_rate` was computed as `rto/(delivered+rto)`. Two different definitions in the same file even when SN aggregates.

**Fix:** weight properly: `rtoRate = sum(rto) / (sum(delivered) + sum(rto)) * 100`.

### 4h. `cash_collected_cod` JOIN can double-count (`compute_daily_metrics_ist`)

```sql
SUM(o2.total) FROM orders o2 JOIN shipments s2 ON s2.order_id=o2.id
WHERE LOWER(payment_type)='cod' AND s2.status='delivered'
```

If 1 order has 2 shipments and both delivered (rare but happens with split fulfillments), `o2.total` is summed twice. **Fix:** use `EXISTS` pattern:
```sql
SUM(o2.total) FROM orders o2 WHERE LOWER(payment_type)='cod'
  AND EXISTS (SELECT 1 FROM shipments s WHERE s.order_id=o2.id AND s.status='delivered')
```

### 4i. `get_hourly_metrics`: ad spend pro-rated by hour-share of orders is misleading

```sql
'ad_spend': CASE WHEN total_day_orders > 0 THEN ROUND(daily_ad_spend * od.total_orders::numeric / total_day_orders, 2) ELSE 0 END
```

Distributes the daily ad spend across hours proportionally to that hour's order count. Implies "ad spend in hour X" = "share of daily spend attributable to that hour's orders". That's a *justification* of attribution, not actual hourly spend. Meta serves ads continuously; spend is real per minute, not gated to when Shopify orders happen.

**Fix:** either pull hourly Meta insights (Meta's API supports `time_increment=1`) — or label this field "Spend (proportional)" so users don't think it's actual.

---

## 5. Date-Filter Blindness (KPIs that don't change when you change the duration)

Verified by reading `renderOverview`:

| KPI | Lines | Why it doesn't filter |
|---|---|---|
| Shipping Health → Delivery Rate, Avg Time, Delivered, RTO count, NDR count | 2762-2773 | reads `DATA.shipments` (loaded once, all-time) |
| Cash Position → Prepaid Collected (when no `cashPrepaid` history) | 2850 | falls back to all-time proportion |
| Cash Position → COD Collected | 2851 | reads `data` but `data.cashCOD` is order-date-anchored, not remit-date-anchored — so today shows ₹0 |

Same bug appears across **renderProducts**, **renderAds**, **renderShipping**, **renderSettlements** — each reads `DATA.X` directly without applying `dur`. I won't enumerate every one; the architectural fix in §6 covers all of them.

---

## 6. The Architectural Fix

**Problem:** the dashboard treats `daily_metrics` (anchored to `order_date`) as the universe, and any KPI not derivable from that universe falls back to all-time numbers from `DATA.shipments` / `DATA.payments` etc.

**Proposed model:** Expose three separate filterable universes per chart.

```
ORDER UNIVERSE:    filter by orders.order_date
SHIPMENT UNIVERSE: filter by shipments.shipped_at (or delivered_at for delivery KPIs)
CASH UNIVERSE:     filter by payments.created_at, settlements.settled_at, cod_remittances.remittance_date
```

Each KPI declares which universe it lives in. The duration selector applies to *each universe independently using the same start/end dates*.

Concretely:
- Add a `getFilteredShipments(dur)` companion to `getFilteredData(dur)` that returns shipments where the appropriate timestamp falls in the window.
- Add a `getFilteredCashEvents(dur)` for COD remits, settlements, payments.
- Each render function picks the right universe(s).
- Label each card with a tiny indicator (📦 if filtered by shipping date, 💰 if by cash date, 📋 if by order date). Users can see at a glance which world they're looking at.

This is a one-day refactor. The payoff: every KPI tells the truth for its date filter.

**Bonus**: add a "**Cohort Mode**" toggle that switches the entire dashboard to "orders placed in window, projected through their full lifecycle" — useful for marketing attribution but explicitly imputed where data is incomplete (e.g., orders <7 days old assumed to follow Flanq's historical 57% delivery rate).

---

## 7. Quick-Win Fix Queue

Ranked by (impact × ease):

| # | Fix | Where | Effort |
|---|---|---|---|
| 1 | Drop `* delivery_rate/100` from `cogs_per_attempt` in `compute_unit_economics` | RPC | 1 line |
| 2 | Fix RTO cost double-subtraction: `total_rto_cost = SUM(rto_cost)` only | `compute_daily_metrics_ist` | 1 line |
| 3 | Use real GST: `SUM(oi.gst_cost * oi.quantity)` instead of `SUM(oi.cogs * oi.quantity * 0.18)` | `compute_daily_metrics_ist` | 1 line |
| 4 | Make Shipping Health card filter by `dur` | `index.html` 2762-2782 | ~20 lines |
| 5 | Make COD Collected/Pending filter by `cod_remittance_date` instead of `order_date` proportion | `index.html` 2850-2856, 4603-4615 | ~20 lines |
| 6 | Standardize RTO Rate to `RTO / (Delivered + RTO)` everywhere | every render fn + 2 RPCs | ~5 lines per site |
| 7 | Fix `DATA.orders.codOrders/prepaidOrders` case-sensitivity | `index.html` 2397-98 | 2 lines |
| 8 | Add `gross_revenue_cod` and `gross_revenue_prepaid` to `daily_metrics` table; replace `codRevenue` proportion math | DDL + RPC + index.html | ~30 lines |
| 9 | Build `getFilteredShipments(dur)` and `getFilteredCashEvents(dur)` helpers + thread through every render fn | index.html | ~150 lines (architectural) |
| 10 | Add CHECK constraint on `orders.payment_type IN ('cod','prepaid','online')` lowercase only | DDL | 1 line |
| 11 | Replace `compute_unit_economics`' hardcoded ₹110 fallback with brand average | RPC | 2 lines |

I have not deployed any of these. They're all changes to your live data layer and a couple of them affect how every KPI renders — your call on order and pace.

---

## 9. SHIPPED 2026-05-11 — all 11 fixes deployed

| # | Status | Migration / file |
|---|---|---|
| 1 | ✅ shipped | `compute_unit_economics` redeploy — dropped `* delivery_rate/100` from `cogs_per_attempt` (RTO inventory comes back; not consumed) |
| 2 | ✅ shipped | `compute_daily_metrics_ist` redeploy — `total_rto_cost = SUM(s.rto_cost)` only (forward leg already in `total_shipping`) |
| 3 | ✅ shipped | `compute_daily_metrics_ist` redeploy — `SUM(oi.gst_cost * oi.quantity)` (real per-product GST, no more flat 18%) |
| 4 | ✅ shipped | `index.html` Shipping Health card now uses `getFilteredShipments(dur, 'delivered_at')` etc. — responds to duration selector. Each KPI anchors on its own event date (delivered → `delivered_at`, RTO → `rto_initiated_at`, NDR → `last_tracking_update`). |
| 5 | ✅ shipped | `index.html` Cash Position now reads from `cod_remittances` table filtered by `remittance_date`, plus uses the new `daily_metrics.gross_revenue_cod` column instead of order-count proportion math |
| 6 | ✅ shipped | `_canonicalRtoRate(delivered, rto)` helper added in `index.html`; replaces all 5 sites that had different RTO formulas. Per-carrier `enrichBucket`/`enrichBkt` also standardized: rate is now over RESOLVED shipments (`delivered + rto`), not over `total` (which inflated denominator with in-transit). |
| 7 | ✅ shipped | `index.html` `DATA.orders.codOrders/prepaidOrders` now case-insensitive. (Also normalized DB itself — see #10.) |
| 8 | ✅ shipped | Migration `add_gross_revenue_split_to_daily_metrics` adds `gross_revenue_cod` and `gross_revenue_prepaid` columns; `compute_daily_metrics_ist` populates them; recomputed all 313 historical days. Dashboard reads them directly. |
| 9 | ✅ shipped | `getFilteredShipments(dur, anchorCol)` and `getFilteredCashEvents(dur)` helpers added next to `getFilteredData`. Three universes (ORDER / SHIPMENT / CASH) now first-class. |
| 10 | ✅ shipped | Migration `normalize_payment_type_and_aggregator_case` lowercased 13,895 `'COD'` and 4,728 `'Prepaid'` orders + 7,072 `'Velocity'` shipments; added CHECK constraints on both columns to lock the convention going forward. |
| 11 | ✅ shipped | `compute_unit_economics` redeploy — uses brand-average `shipping_cost` (computed from real Velocity-backfilled rows, exposed as `brand_avg_shipping_cost` in the RPC output) instead of hardcoded ₹110. |

### Recompute pass
After RPC fixes #1-3, #8: ran `compute_daily_metrics_ist(d)` for every date with orders (313 dates, 2025-05-12 → 2026-05-11). New aggregate totals:

| Metric | Value |
|---|---|
| Gross revenue (year) | ₹3.12 Cr |
| Gross revenue COD | ₹2.25 Cr (72%) |
| Gross revenue Prepaid | ₹0.87 Cr (28%) |
| COGS | ₹0.99 Cr |
| GST cost (real, per-product) | ₹17.1 L |
| Net profit (post-correction) | ₹1.27 Cr |

### Validations
- `node --check` on the dashboard's largest `<script>` block: clean, no syntax errors.
- All 313 daily_metrics rows now have non-NULL `gross_revenue_cod` and `gross_revenue_prepaid`.
- payment_type CHECK and aggregator CHECK constraints verified by repeating the inventory query post-migration: only `cod`/`prepaid` and only `velocity`/`itl` present.

### What's still open (not in the original §7 list)
- **ITL backfill** — `shipping-sync-itl` is deployed but ITL rows in `shipments` are not enriched the way the Velocity rows are (delivered_at / is_rto / shipping_cost / cod_remittance_date often stale). Until done, any RTO/delivery KPI is Velocity-portion-accurate but ITL-portion-stale. Suggested next: scrape ITL dashboard or hit ITL's API endpoint with `ITL_ACCESS_TOKEN` for full historical export, then write `backfill_itl.py` mirroring `backfill_velocity.py`.
- **CAC vs CPP rename** (audit §4e) — still tracked in `compute_unit_economics` as `blended_cpp_ex_gst`; rename to `blended_cpp` and add `blended_cac` from Shopify-side data. Not in the §7 list, deferred.
- **Webhook-shopify v7** also strips `#` from order_number on write (today's separate fix). webhook-razorpay is at v4 (rejects bad sigs). Both behaviors documented in this file's earlier sections.

---

## 8. My v5 mistake + what I changed to prevent recurrence

In webhook-shopify v5 (deployed earlier today as part of fixing the `products.cogs` column-name bug), I added `* (it.quantity || 1)` to the cogs assignment. This contradicted the schema convention (cogs stored per-unit, multiplied by qty in aggregations).

What I missed: I should have read at least one of the SQL aggregations before changing the writer. Reading the column type alone was insufficient — I needed to know how every reader interprets the column.

**Mitigation deployed:**
1. Reverted to per-unit in **v6** (live now; explicit comment in code documenting the convention).
2. Ran `refresh_order_items_cogs()` to re-normalize 19,317 existing rows. State is consistent.
3. Added §3e to this doc so any future change to `cogs` semantics has to acknowledge all five readers.
4. Updating `~/.claude/projects/.../memory/` so any future session that touches `order_items.cogs` will see the convention before editing.

The `validate_daily_metrics_internal` RPC was already running in cron (job 17) and would have flagged the COGS divergence within a day, but the lesson is that I should not have introduced the regression in the first place.
