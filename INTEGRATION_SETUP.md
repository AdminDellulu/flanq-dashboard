# Integration Setup — per Flanq store

This dashboard reads from Supabase. All integration plumbing sits **upstream** inside each store's Supabase project (webhooks, edge functions, n8n workflows). The dashboard itself has no Shopify/Meta/Razorpay/Velocity/ITL tokens — only the Supabase URL + anon key per store, already configured in `index.html`'s `STORES` array.

Use this doc every time you bring a new store online.

## Prerequisites

- New Supabase project created, schema ported (20 migrations from `dellulu-finance`)
- Store entry added to `STORES` array in `index.html`
- Shopify, Meta Ads, Razorpay, Velocity, ITL accounts for the new store

---

## 1. Shopify

**Where:** Shopify admin → Settings → Notifications (webhooks) + Apps → Develop apps → Create app.

- [ ] Create a custom app with API scopes: `read_orders`, `read_products`, `read_fulfillments`, `read_inventory`, `read_customers`.
- [ ] Copy the **Admin API access token** — save as Supabase secret: `SHOPIFY_ADMIN_TOKEN`.
- [ ] Copy the **store domain** (e.g. `store2.myshopify.com`) — save as `SHOPIFY_STORE_DOMAIN`.
- [ ] Configure webhooks (JSON format, latest API version):
  - `orders/create` → `https://<supabase-ref>.supabase.co/functions/v1/webhook-shopify`
  - `orders/updated` → same URL
  - `orders/cancelled` → same URL
  - `fulfillments/create` → same URL
  - `fulfillments/update` → same URL
- [ ] Copy the shared webhook secret → save as `SHOPIFY_WEBHOOK_SECRET`.

**Verify:** place a test order in Shopify → within 10s it should appear in the Flanq Supabase `orders` table.

---

## 2. Meta Ads

**Where:** Meta Business Manager + Meta Ads Manager.

- [ ] Get the ad account ID (format `act_1234567890`) — save as `META_AD_ACCOUNT_ID`.
- [ ] Generate a long-lived user access token (System User token recommended) with `ads_read`, `business_management` scopes — save as `META_ACCESS_TOKEN`.
- [ ] Ensure `app.ordscale.com` / `preprod.ordscale.com` are allowlisted (already done for Dellulu app 1850133595650386, reuse that app or create a new one for Flanq).
- [ ] Trigger `sync-meta-ads` edge function via cron (every 6 hours) or n8n workflow.

**Verify:** invoke `sync-meta-ads` manually → `ad_spend` table should populate with today's spend broken down by campaign.

---

## 3. Razorpay

**Where:** Razorpay dashboard → Settings → Webhooks + API Keys.

- [ ] Generate API keys (Live mode). Save as `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`.
- [ ] Create webhook: `https://<supabase-ref>.supabase.co/functions/v1/webhook-razorpay`
  - Events: `payment.captured`, `payment.failed`, `order.paid`, `refund.created`, `settlement.processed`.
  - Set secret → save as `RAZORPAY_WEBHOOK_SECRET`.
- [ ] Make a ₹1 test payment → `payments` table should get the row within seconds.

---

## 4. Velocity Express

**Where:** Velocity merchant dashboard / API docs.

- [ ] Get username + password for the store's Velocity account.
- [ ] Save as `VELOCITY_USERNAME` + `VELOCITY_PASSWORD` Supabase secrets.
- [ ] Cron `shipping-sync-velocity` every 1 hour.

**Verify:** invoke manually → new AWBs appear in `shipments` with `shipping_partner='velocity'`.

---

## 5. iThink Logistics (ITL)

**Where:** ITL merchant panel (my.ithinklogistics.com).

- [ ] Get API access token (access token may drift between `pre-alpha` and `my` hosts — see CLAUDE.md note). Save as `ITL_ACCESS_TOKEN`.
- [ ] Save the store's `client_id` + `client_secret` if required.
- [ ] Cron `shipping-sync-itl` every 1 hour.

**Known issue** (per org memory): ITL has an "Access Token Not Match" error on the `pre-alpha` host — use `my` host when the pre-alpha token fails.

**Verify:** invoke manually → new AWBs appear in `shipments` with `shipping_partner='itl'`.

---

## 6. Supabase secrets reference

Set these in Supabase dashboard → Project Settings → Edge Functions → Environment Variables, one per store:

```
SHOPIFY_ADMIN_TOKEN=
SHOPIFY_STORE_DOMAIN=
SHOPIFY_WEBHOOK_SECRET=

META_AD_ACCOUNT_ID=
META_ACCESS_TOKEN=

RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET=

VELOCITY_USERNAME=
VELOCITY_PASSWORD=

ITL_ACCESS_TOKEN=
ITL_CLIENT_ID=
ITL_CLIENT_SECRET=
```

## 7. Port edge functions

The 13 integration edge functions from `dellulu-finance` need to be ported to the new Flanq Supabase project:

- `sync-shopify-orders`, `webhook-shopify`, `backfill-shopify-ids`
- `sync-meta-ads`
- `sync-razorpay`, `webhook-razorpay`
- `sync-shipping`, `sync-tracking`, `shipping-sync-velocity`, `shipping-sync-itl`, `shipping-aggregate-metrics`, `probe-itl-api`
- `reconcile`, `watchdog`

Port by running `supabase functions download <slug>` against the `dellulu-finance` project, then `supabase functions deploy <slug> --project-ref <flanq-ref>`. Or use the MCP (`get_edge_function` → `deploy_edge_function`).

## 8. Go-live checklist

- [ ] All 5 integrations configured (§1–§5)
- [ ] All edge functions deployed (§7)
- [ ] First test order flows through Shopify → webhook → Supabase → visible in dashboard
- [ ] Store selector in dashboard header shows new store (if ≥2 stores)
- [ ] `pipeline_health_log` shows all 6 pipelines healthy after first full sync cycle
