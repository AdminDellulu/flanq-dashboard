#!/usr/bin/env python3
"""
Backfill Shopify Admin Order Export CSVs into the Flanq Supabase project.

Shopify exports orders as a "one row per line item" CSV (Admin -> Orders -> Export).
The first row of each order carries the order-level metadata; subsequent rows for the
same order have those fields blank. This importer:

  1. Reads all CSVs in the input folder matching '*shopify*order*.csv'
  2. Groups rows by 'Name' (order number) so we get one record per order with N line items
  3. UPSERTs the orders by `order_number` (refreshing lifecycle fields like cancelled/refunded/tags)
  4. INSERTs line items for orders that don't already have them, looking up product_id
     by SKU then by name, and pulling cost_per_item from products to set order_items.cogs
     PER-UNIT (per the schema convention — see KPI_AUDIT.md §3e and the cogs memory rule)

Usage
-----
    pip install 'psycopg[binary]'  # one-time
    export DATABASE_URL='postgresql://postgres.<ref>:<pwd>@aws-1-ap-south-1.pooler.supabase.com:5432/postgres'

    python3 scripts/backfill_shopify_csv.py --dry-run "/path/to/csv/folder"
    python3 scripts/backfill_shopify_csv.py            "/path/to/csv/folder"

Safety
------
- All writes happen in a single transaction. Failure rolls back.
- For orders that already exist (matched by order_number):
    - order-level fields are upserted (so cancellations, refunds, tags get refreshed)
    - line items are NOT touched (the webhook / sync-shopify-orders is the source of
      truth for line items; we don't risk creating dupes by re-inserting)
- For orders that don't exist:
    - order is inserted, line items are inserted with synthetic shopify_line_item_id
      of `<shopify_order_id>-<idx>`
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg import sql
except ImportError:
    sys.exit("psycopg not installed. Run: pip install 'psycopg[binary]'")

BRAND_ID = "flanq"
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


NULLISH = {"", "n/a", "na", "none", "null", "-"}


def s(v: Any) -> str | None:
    if v is None:
        return None
    t = str(v).strip()
    return None if t.lower() in NULLISH else t


def num(v: Any) -> Decimal | None:
    t = s(v)
    if t is None:
        return None
    t = t.replace(",", "")
    try:
        return Decimal(t)
    except (InvalidOperation, ValueError):
        return None


def ts(v: Any) -> datetime | None:
    """Shopify exports timestamps as 'YYYY-MM-DD HH:MM:SS +0530'."""
    t = s(v)
    if t is None:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(t, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def detect_payment_type(payment_method: str | None, tags: str | None) -> str:
    blob = " ".join(filter(None, [payment_method, tags])).lower()
    if "cod" in blob or "cash on delivery" in blob:
        return "cod"
    return "prepaid"


def find_csvs(folder: Path) -> list[Path]:
    matches = list(folder.glob("*shopify*order*.csv")) + list(folder.glob("*shopify*orders*.csv"))
    seen, out = set(), []
    for m in sorted(matches):
        if m not in seen:
            seen.add(m); out.append(m)
    return out


def parse_files(paths: list[Path]) -> tuple[dict[str, dict], list[dict]]:
    orders: dict[str, dict] = {}
    line_items: list[dict] = []
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = s(row.get("Name"))
                if name is None:
                    continue
                created = s(row.get("Created at"))
                if created and name not in orders:
                    paymethod = s(row.get("Payment Method"))
                    tags = s(row.get("Tags"))
                    pay = detect_payment_type(paymethod, tags)
                    total = num(row.get("Total")) or Decimal(0)
                    discount = num(row.get("Discount Amount")) or Decimal(0)
                    pg_fee = (total * Decimal("0.02")).quantize(Decimal("0.01")) if pay == "prepaid" else Decimal(0)
                    sid_raw = s(row.get("Id"))
                    sid_int = int(sid_raw) if sid_raw and sid_raw.isdigit() else None
                    orders[name] = {
                        "shopify_order_id": sid_int,
                        "order_number": name.lstrip("#"),
                        "order_date": ts(created),
                        "customer_name": s(row.get("Billing Name")) or s(row.get("Shipping Name")) or "Unknown",
                        "customer_email": s(row.get("Email")),
                        "customer_phone": s(row.get("Phone")) or s(row.get("Billing Phone")) or s(row.get("Shipping Phone")),
                        "shipping_city": s(row.get("Shipping City")),
                        "shipping_state": s(row.get("Shipping Province")),
                        "shipping_pincode": s(row.get("Shipping Zip")),
                        "payment_type": pay,
                        "payment_method": paymethod,
                        "payment_gateway_fee": pg_fee,
                        "subtotal": num(row.get("Subtotal")) or Decimal(0),
                        "discount_amount": discount,
                        "tax": num(row.get("Taxes")) or Decimal(0),
                        "shipping_charge": num(row.get("Shipping")) or Decimal(0),
                        "total": total,
                        "revenue": total - discount,
                        "discount_code": s(row.get("Discount Code")),
                        "refunded_amount": num(row.get("Refunded Amount")) or Decimal(0),
                        "outstanding_balance": num(row.get("Outstanding Balance")) or Decimal(0),
                        "fulfillment_status": s(row.get("Fulfillment Status")) or "unfulfilled",
                        "financial_status": s(row.get("Financial Status")) or "pending",
                        "tags": tags,
                        "risk_level": s(row.get("Risk Level")),
                        "brand_id": BRAND_ID,
                    }
                li_qty = s(row.get("Lineitem quantity"))
                li_name = s(row.get("Lineitem name"))
                if li_qty and li_name:
                    qty = int(num(li_qty) or 1)
                    price = num(row.get("Lineitem price")) or Decimal(0)
                    line_items.append({
                        "order_name": name,
                        "sku": s(row.get("Lineitem sku")),
                        "product_name": li_name,
                        "variant_name": None,
                        "quantity": qty,
                        "unit_price": price,
                        "line_total": price * qty,
                        "discount_amount": num(row.get("Lineitem discount")) or Decimal(0),
                    })
    return orders, line_items


@contextmanager
def db(url: str, dry_run: bool):
    with psycopg.connect(url, autocommit=False) as conn:
        try:
            yield conn
            if dry_run:
                conn.rollback()
                print("\n[dry-run] rolled back, no changes committed.")
            else:
                conn.commit()
                print("\n[commit] transaction committed.")
        except Exception:
            conn.rollback()
            raise


def snapshot(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM orders WHERE brand_id='flanq') AS total,
              (SELECT COUNT(*) FROM orders WHERE brand_id='flanq' AND order_number LIKE 'F-%') AS f_orders,
              (SELECT COUNT(*) FROM orders WHERE brand_id='flanq' AND order_number LIKE 'WC-%') AS wc_orders,
              (SELECT COUNT(*) FROM orders WHERE brand_id='flanq' AND financial_status IN ('cancelled','voided')) AS cancelled,
              (SELECT COUNT(*) FROM orders WHERE brand_id='flanq' AND COALESCE(refunded_amount,0) > 0) AS refunded,
              (SELECT COUNT(*) FROM order_items WHERE brand_id='flanq') AS items,
              (SELECT MIN(order_date)::date::text FROM orders WHERE brand_id='flanq') AS first_order,
              (SELECT MAX(order_date)::date::text FROM orders WHERE brand_id='flanq') AS last_order;
            """
        )
        cols = [d.name for d in cur.description]
        return dict(zip(cols, cur.fetchone()))


def stage_orders(conn, orders: dict[str, dict]) -> int:
    if not orders:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TEMP TABLE _stage_shop_orders (
              shopify_order_id bigint,
              order_number text PRIMARY KEY,
              order_date timestamptz,
              customer_name text, customer_email text, customer_phone text,
              shipping_city text, shipping_state text, shipping_pincode text,
              payment_type text, payment_method text, payment_gateway_fee numeric,
              subtotal numeric, discount_amount numeric, tax numeric, shipping_charge numeric,
              total numeric, revenue numeric, discount_code text,
              refunded_amount numeric, outstanding_balance numeric,
              fulfillment_status text, financial_status text,
              tags text, risk_level text,
              brand_id text NOT NULL
            ) ON COMMIT DROP;
            """
        )
        cols = [
            "shopify_order_id","order_number","order_date","customer_name","customer_email","customer_phone",
            "shipping_city","shipping_state","shipping_pincode","payment_type","payment_method","payment_gateway_fee",
            "subtotal","discount_amount","tax","shipping_charge","total","revenue","discount_code",
            "refunded_amount","outstanding_balance","fulfillment_status","financial_status","tags","risk_level","brand_id"
        ]
        with cur.copy(
            sql.SQL("COPY _stage_shop_orders ({}) FROM STDIN").format(
                sql.SQL(", ").join(sql.Identifier(c) for c in cols)
            )
        ) as cp:
            for o in orders.values():
                cp.write_row([o.get(c) for c in cols])
    return len(orders)


def upsert_orders(conn) -> dict[str, int]:
    """
    Dedup primarily on shopify_order_id (durable Shopify-side key), falling back
    to order_number. Historical pipeline renamed some WC-* orders to F-* in the DB
    while keeping the original shopify_order_id, so order_number alone misses them.
    Existing rows are updated in place; the DB's order_number is preserved (don't
    rename F-1449 back to WC-1449).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH upd AS (
              UPDATE orders o
                 SET shopify_order_id    = COALESCE(o.shopify_order_id, s.shopify_order_id),
                     financial_status    = COALESCE(s.financial_status, o.financial_status),
                     fulfillment_status  = COALESCE(s.fulfillment_status, o.fulfillment_status),
                     refunded_amount     = COALESCE(s.refunded_amount, o.refunded_amount),
                     outstanding_balance = COALESCE(s.outstanding_balance, o.outstanding_balance),
                     discount_code       = COALESCE(o.discount_code, s.discount_code),
                     tags                = COALESCE(s.tags, o.tags),
                     risk_level          = COALESCE(s.risk_level, o.risk_level),
                     payment_method      = COALESCE(s.payment_method, o.payment_method),
                     customer_phone      = COALESCE(o.customer_phone, s.customer_phone),
                     shipping_city       = COALESCE(o.shipping_city, s.shipping_city),
                     shipping_state      = COALESCE(o.shipping_state, s.shipping_state),
                     shipping_pincode    = COALESCE(o.shipping_pincode, s.shipping_pincode),
                     updated_at          = now()
                FROM _stage_shop_orders s
               WHERE o.brand_id = 'flanq'
                 AND (
                   (s.shopify_order_id IS NOT NULL AND o.shopify_order_id = s.shopify_order_id)
                   OR o.order_number = s.order_number
                 )
              RETURNING o.id
            )
            SELECT COUNT(*) FROM upd;
            """
        )
        updated = cur.fetchone()[0]

        cur.execute(
            """
            WITH ins AS (
              INSERT INTO orders (
                shopify_order_id, order_number, order_date, customer_name, customer_email, customer_phone,
                shipping_city, shipping_state, shipping_pincode, payment_type, payment_method, payment_gateway_fee,
                subtotal, discount_amount, tax, shipping_charge, total, revenue, discount_code,
                refunded_amount, outstanding_balance, fulfillment_status, financial_status, tags, risk_level,
                brand_id, created_at, updated_at
              )
              SELECT
                s.shopify_order_id, s.order_number, s.order_date, s.customer_name, s.customer_email, s.customer_phone,
                s.shipping_city, s.shipping_state, s.shipping_pincode, s.payment_type, s.payment_method, s.payment_gateway_fee,
                s.subtotal, s.discount_amount, s.tax, s.shipping_charge, s.total, s.revenue, s.discount_code,
                s.refunded_amount, s.outstanding_balance, s.fulfillment_status, s.financial_status, s.tags, s.risk_level,
                s.brand_id, now(), now()
              FROM _stage_shop_orders s
              WHERE NOT EXISTS (
                SELECT 1 FROM orders o
                 WHERE o.brand_id = 'flanq'
                   AND (
                     (s.shopify_order_id IS NOT NULL AND o.shopify_order_id = s.shopify_order_id)
                     OR o.order_number = s.order_number
                   )
              )
              RETURNING 1
            )
            SELECT COUNT(*) FROM ins;
            """
        )
        inserted = cur.fetchone()[0]
    return {"updated": updated, "inserted": inserted}


def stage_and_insert_line_items(conn, line_items: list[dict]) -> int:
    if not line_items:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TEMP TABLE _stage_shop_items (
              order_name text,
              sku text,
              product_name text,
              variant_name text,
              quantity int,
              unit_price numeric,
              line_total numeric,
              discount_amount numeric
            ) ON COMMIT DROP;
            """
        )
        with cur.copy(
            "COPY _stage_shop_items (order_name, sku, product_name, variant_name, quantity, unit_price, line_total, discount_amount) FROM STDIN"
        ) as cp:
            for li in line_items:
                cp.write_row([
                    li["order_name"], li["sku"], li["product_name"], li["variant_name"],
                    li["quantity"], li["unit_price"], li["line_total"], li["discount_amount"]
                ])

        cur.execute(
            """
            WITH order_map AS (
              SELECT o.id AS order_id, o.shopify_order_id, '#' || o.order_number AS hashname, o.order_number AS plainname
                FROM orders o
                JOIN _stage_shop_items s ON (s.order_name = '#' || o.order_number OR s.order_name = o.order_number)
                WHERE o.brand_id = 'flanq'
                GROUP BY o.id, o.shopify_order_id, o.order_number
            ),
            existing AS (
              SELECT DISTINCT order_id FROM order_items WHERE brand_id='flanq'
            ),
            target_orders AS (
              SELECT om.order_id, om.shopify_order_id, om.hashname, om.plainname
                FROM order_map om
                WHERE NOT EXISTS (SELECT 1 FROM existing e WHERE e.order_id = om.order_id)
            ),
            numbered AS (
              SELECT
                t.order_id, t.shopify_order_id,
                s.sku, s.product_name, s.variant_name, s.quantity, s.unit_price, s.line_total, s.discount_amount,
                ROW_NUMBER() OVER (PARTITION BY t.order_id ORDER BY s.product_name, s.sku) AS idx
              FROM target_orders t
              JOIN _stage_shop_items s ON (s.order_name = t.hashname OR s.order_name = t.plainname)
            ),
            with_product AS (
              SELECT n.*,
                     COALESCE(
                       (SELECT p.id FROM products p WHERE LOWER(p.sku) = LOWER(n.sku) AND p.brand_id='flanq' LIMIT 1),
                       (SELECT p.id FROM products p WHERE p.brand_id='flanq' AND LOWER(p.name) = LOWER(n.product_name) LIMIT 1),
                       (SELECT p.id FROM products p WHERE p.brand_id='flanq' AND LOWER(p.name) ILIKE '%' || LOWER(SPLIT_PART(n.product_name,'™',1)) || '%' LIMIT 1)
                     ) AS product_id
              FROM numbered n
            ),
            ins AS (
              INSERT INTO order_items (
                order_id, shopify_line_item_id, product_id, product_name, variant_name,
                sku, quantity, unit_price, line_total, discount_amount,
                cogs, gst_cost, packaging_cost, brand_id, created_at
              )
              SELECT
                wp.order_id,
                COALESCE(wp.shopify_order_id::text, wp.order_id::text) || '-' || wp.idx::text AS shopify_line_item_id,
                wp.product_id, wp.product_name, wp.variant_name,
                wp.sku, wp.quantity, wp.unit_price, wp.line_total, wp.discount_amount,
                ROUND(COALESCE(p.cost_per_item, 0)::numeric, 2) AS cogs,
                ROUND((COALESCE(p.cost_per_item,0) * (COALESCE(p.gst_percent,18)/100.0))::numeric, 2) AS gst_cost,
                COALESCE(p.packaging_cost, 25) AS packaging_cost,
                'flanq', now()
              FROM with_product wp
              LEFT JOIN products p ON p.id = wp.product_id
              ON CONFLICT DO NOTHING
              RETURNING 1
            )
            SELECT COUNT(*) FROM ins;
            """
        )
        return cur.fetchone()[0]


def main():
    ap = argparse.ArgumentParser(description="Backfill Shopify Admin Order Export CSVs into Flanq Supabase.")
    ap.add_argument("folder", help="folder containing the Shopify export CSVs")
    ap.add_argument("--dry-run", action="store_true", help="parse + run UPSERTs in a transaction, then ROLLBACK")
    args = ap.parse_args()

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        sys.exit(f"not a directory: {folder}")

    env = load_env(ENV_PATH)
    db_url = env.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("DATABASE_URL not set. See backfill_velocity.py for how to get the Session-pooler URI.")

    csvs = find_csvs(folder)
    if not csvs:
        sys.exit(f"No CSVs matching '*shopify*order*.csv' in {folder}")
    print(f"Folder: {folder}")
    print(f"Found {len(csvs)} CSV file(s):")
    for c in csvs:
        print(f"  {c.name}  ({c.stat().st_size:,} bytes)")

    print("\nParsing...")
    orders, line_items = parse_files(csvs)
    print(f"  Distinct orders: {len(orders):,}")
    print(f"  Total line items: {len(line_items):,}")
    if not orders:
        sys.exit("No orders parsed — exiting.")

    with db(db_url, dry_run=args.dry_run) as conn:
        before = snapshot(conn)
        print("\n--- BEFORE ---")
        for k, v in before.items():
            print(f"  {k:<14} {v}")

        staged = stage_orders(conn, orders)
        print(f"\nStaged {staged:,} orders.")
        result = upsert_orders(conn)
        print(f"  orders updated:  {result['updated']:,}")
        print(f"  orders inserted: {result['inserted']:,}")

        items_inserted = stage_and_insert_line_items(conn, line_items)
        print(f"  line items inserted (only for orders w/ no existing items): {items_inserted:,}")

        after = snapshot(conn)
        print("\n--- AFTER ---")
        for k, v in after.items():
            try:
                d_before = before[k]
                if isinstance(v, (int, float)) and isinstance(d_before, (int, float)):
                    delta = v - d_before
                    deltastr = f"  ({'+' if delta > 0 else ''}{delta:g})" if delta else ""
                else:
                    deltastr = ""
            except Exception:
                deltastr = ""
            print(f"  {k:<14} {v}{deltastr}")


if __name__ == "__main__":
    main()
