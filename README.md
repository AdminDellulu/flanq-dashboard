# Flanq Finance Dashboard

Real-time e-commerce analytics dashboard for Flanq stores, powered by Supabase.

## Live Dashboard

(enable GitHub Pages after first push — expected URL: `https://<owner>.github.io/flanq-dashboard/`)

## Multi-store architecture

Supports multiple Flanq stores from one deployment. Each store = its own Supabase project (same schema); the frontend switches between them via a header selector.

Active stores are configured in one place in `index.html`:

```js
const STORES = [
  { id: 'flanq-1', label: 'Flanq — Store 1', url: '…', key: '…' },
  { id: 'flanq-2', label: 'Flanq — Store 2', url: '…', key: '…' }   // add Store 2 here when ready
];
```

To add a second store: create a new Supabase project, apply the same schema, append an entry to `STORES`, commit, redeploy.

Active-store resolution order: `?store=<id>` URL param → `localStorage['flanq_active_store']` → first entry. User choice via selector persists.

## Tech stack

- Single-file HTML/CSS/JS
- Chart.js for visualisations
- Supabase REST API for data
- GitHub Pages for hosting

## Upstream integrations

Each store's Supabase project must have these configured to write into it:

| Integration | Action |
|---|---|
| Shopify | Webhook → `webhook-shopify` edge function; Admin API token |
| Meta Ads | `sync-meta-ads` or n8n workflow with ad account ID + token |
| Razorpay | Webhook → `webhook-razorpay`; key_id / key_secret as Supabase secrets |
| Velocity | `sync-shipping` / `shipping-sync-velocity` with Velocity creds |
| ITL | `shipping-sync-itl` with ITL API creds |

See `INTEGRATION_SETUP.md` for the per-store credentials swap checklist.
