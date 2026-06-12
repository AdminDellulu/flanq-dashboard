#!/usr/bin/env python3
"""
Backfill Velocity Express CSV exports into the Flanq Supabase project.

Velocity exports three CSVs from the dashboard ("Mukshree D-..." prefix because
the seller of record on Velocity is the warehouse entity, Mukshree Design Studio):

  1. <brand>-Order Report-<ts>.csv               -> updates/inserts shipments
  2. <brand>-COD Remittance Detailed Report-*.csv -> stamps COD remittance on shipments
  3. <brand>-COD Settlement Report-*.csv          -> inserts cod_remittances rows

Match key for shipments is awb_number (UNIQUE in DB). For unmatched AWBs we
also try to attach an order_id by stripping the leading '#' from "#F-NNNNN" and
joining on orders.order_number.

Usage
-----
    # one-time: paste your pooler URI into scripts/.env as DATABASE_URL=
    # (Supabase dashboard -> Settings -> Database -> "Connection string" -> URI)
    pip install 'psycopg[binary]'  # one-time

    # preview only (no writes):
    python3 scripts/backfill_velocity.py --dry-run "/path/to/csv/folder"

    # commit:
    python3 scripts/backfill_velocity.py "/path/to/csv/folder"

Safety
------
- All writes happen in a single transaction. Failure rolls back.
- Existing shipments are UPDATED only on the freight/RTO/COD/dest fields the
  CSV authoritatively owns. Identity columns (id, brand_id, order_id) are not
  touched on update.
- Pre/post counts are printed so you can see what changed.
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
from typing import Any, Iterable

try:
    import psycopg
    from psycopg import sql
except ImportError:
    sys.exit("psycopg not installed. Run: pip install 'psycopg[binary]'")

BRAND_ID = "flanq"
AGGREGATOR = "velocity"
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"


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


_DATETIME_FORMATS = (
    "%d/%m/%y %H:%M",
    "%d/%m/%y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def ts(v: Any) -> datetime | None:
    t = s(v)
    if t is None:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def order_number_clean(raw: str | None) -> str | None:
    """Velocity gives '#F-19658'; DB stores 'F-19658'."""
    t = s(raw)
    if t is None:
        return None
    return t.lstrip("#")


def detect_courier(raw_courier: str | None) -> tuple[str | None, str | None]:
    """Velocity courier label -> (label, normalized carrier).
    'Bluedart Standard Prime' -> ('Bluedart Standard Prime', 'BlueDart')
    """
    t = s(raw_courier)
    if t is None:
        return None, None
    low = t.lower()
    for needle, carrier in (
        ("bluedart", "BlueDart"),
        ("delhivery", "Delhivery"),
        ("dtdc", "DTDC"),
        ("xpressbees", "Xpressbees"),
        ("ekart", "Ekart"),
        ("shadowfax", "Shadowfax"),
        ("amazon", "Amazon"),
    ):
        if needle in low:
            return t, carrier
    return t, None


def parse_order_report(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            awb = s(row.get("AWB No"))
            if awb is None:
                continue
            courier_label, carrier = detect_courier(row.get("Courier Company"))
            order_status = s(row.get("Order Status")) or ""
            is_rto = "rto" in order_status.lower() or s(row.get("RTO Initiated Date")) is not None
            yield {
                "awb_number": awb,
                "order_number": order_number_clean(row.get("Order ID")),
                "courier": courier_label,
                "actual_carrier": carrier,
                "shipping_partner": AGGREGATOR,
                "aggregator": AGGREGATOR,
                "status": order_status.lower() or None,
                "is_rto": is_rto,
                "shipped_at": ts(row.get("Order Picked Up Date")) or ts(row.get("Order Pickup Date")),
                "manifested_at": ts(row.get("AWB Assigned Date")),
                "delivered_at": ts(row.get("Order Delivered Date")),
                "rto_initiated_at": ts(row.get("RTO Initiated Date")),
                "rto_delivered_at": ts(row.get("RTO Delivered Date")),
                "estimated_delivery": ts(row.get("Estimated Delivery Date")),
                "shipping_cost": num(row.get("Total Freight Charge")),
                "rto_cost": num(row.get("RTO Charges")),
                "cod_amount": num(row.get("COD Amount")),
                "cod_remittance_date": ts(row.get("COD Remittance Date")),
                "weight_charged": num(row.get("Billed Weight")),
                "charged_weight": num(row.get("Billed Weight")),
                "delivery_zone": s(row.get("Zone")),
                "ndr_count": int(num(row.get("Attempt count")) or 0),
                "ndr_reason": s(row.get("Latest NDR reason")),
                "rto_reason": s(row.get("RTO Reason")),
                "payment_mode": s(row.get("Payment Method")),
                "dest_pincode": s(row.get("Customer Pincode")),
                "dest_city": s(row.get("Customer City")),
                "dest_state": s(row.get("Customer State")),
                "current_location": s(row.get("Current location")),
                "brand_id": BRAND_ID,
            }


def parse_cod_remit_detailed(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            awb = s(row.get("AWB Number"))
            if awb is None:
                continue
            yield {
                "awb_number": awb,
                "cod_remittance_amount": num(row.get("Total Order Amount")),
                "cod_remittance_date": ts(row.get("Remittance Date")),
                "cod_remitted": True,
            }


def parse_cod_settlement(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            sid = s(row.get("Settlement ID"))
            if sid is None:
                continue
            yield {
                "aggregator": AGGREGATOR,
                "remittance_id": sid,
                "remittance_date": (ts(row.get("Transaction Date")) or datetime.now(timezone.utc)).date(),
                "utr": s(row.get("UTR")),
                "cod_remitted": num(row.get("Amount")),
                "remittance_status": s(row.get("Status")),
                "brand_id": BRAND_ID,
            }


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
              (SELECT COUNT(*) FROM shipments)                                  AS shipments_total,
              (SELECT COUNT(*) FROM shipments WHERE awb_number IS NOT NULL)     AS shipments_with_awb,
              (SELECT COUNT(*) FROM shipments WHERE delivered_at IS NOT NULL)   AS delivered,
              (SELECT COUNT(*) FROM shipments WHERE is_rto IS TRUE)             AS rtos,
              (SELECT COUNT(*) FROM shipments WHERE cod_remitted IS TRUE)       AS cod_remitted,
              (SELECT COUNT(*) FROM cod_remittances)                            AS cod_remit_rows,
              (SELECT COALESCE(ROUND(SUM(shipping_cost)::numeric, 0), 0)
                 FROM shipments)                                                AS total_freight,
              (SELECT COALESCE(ROUND(SUM(cod_amount)::numeric, 0), 0)
                 FROM shipments)                                                AS total_cod;
            """
        )
        cols = [d.name for d in cur.description]
        return dict(zip(cols, cur.fetchone()))


def stage_order_report(conn, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TEMP TABLE _stage_velocity_orders (
              awb_number text PRIMARY KEY,
              order_number text,
              courier text,
              actual_carrier text,
              shipping_partner text,
              aggregator text,
              status text,
              is_rto boolean,
              shipped_at timestamptz,
              manifested_at timestamptz,
              delivered_at timestamptz,
              rto_initiated_at timestamptz,
              rto_delivered_at timestamptz,
              estimated_delivery timestamptz,
              shipping_cost numeric,
              rto_cost numeric,
              cod_amount numeric,
              cod_remittance_date timestamptz,
              weight_charged numeric,
              charged_weight numeric,
              delivery_zone text,
              ndr_count integer,
              ndr_reason text,
              rto_reason text,
              payment_mode text,
              dest_pincode text,
              dest_city text,
              dest_state text,
              current_location text,
              brand_id text NOT NULL
            ) ON COMMIT DROP;
            """
        )
        with cur.copy(
            sql.SQL("COPY _stage_velocity_orders ({}) FROM STDIN").format(
                sql.SQL(", ").join(sql.Identifier(c) for c in cols)
            )
        ) as cp:
            for r in rows:
                cp.write_row([r.get(c) for c in cols])
    return len(rows)


def upsert_shipments(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH upd AS (
              UPDATE shipments sh
                 SET courier              = COALESCE(s.courier,              sh.courier),
                     actual_carrier       = COALESCE(s.actual_carrier,       sh.actual_carrier),
                     shipping_partner     = COALESCE(s.shipping_partner,     sh.shipping_partner),
                     aggregator           = COALESCE(s.aggregator,           sh.aggregator),
                     status               = COALESCE(s.status,               sh.status),
                     is_rto               = COALESCE(s.is_rto,               sh.is_rto),
                     shipped_at           = COALESCE(s.shipped_at,           sh.shipped_at),
                     manifested_at        = COALESCE(s.manifested_at,        sh.manifested_at),
                     delivered_at         = COALESCE(s.delivered_at,         sh.delivered_at),
                     rto_initiated_at     = COALESCE(s.rto_initiated_at,     sh.rto_initiated_at),
                     rto_delivered_at     = COALESCE(s.rto_delivered_at,     sh.rto_delivered_at),
                     estimated_delivery   = COALESCE(s.estimated_delivery,   sh.estimated_delivery),
                     shipping_cost        = COALESCE(s.shipping_cost,        sh.shipping_cost),
                     rto_cost             = COALESCE(s.rto_cost,             sh.rto_cost),
                     cod_amount           = COALESCE(s.cod_amount,           sh.cod_amount),
                     weight_charged       = COALESCE(s.weight_charged,       sh.weight_charged),
                     charged_weight       = COALESCE(s.charged_weight,       sh.charged_weight),
                     delivery_zone        = COALESCE(s.delivery_zone,        sh.delivery_zone),
                     ndr_count            = COALESCE(s.ndr_count,            sh.ndr_count),
                     ndr_reason           = COALESCE(s.ndr_reason,           sh.ndr_reason),
                     rto_reason           = COALESCE(s.rto_reason,           sh.rto_reason),
                     payment_mode         = COALESCE(s.payment_mode,         sh.payment_mode),
                     dest_pincode         = COALESCE(s.dest_pincode,         sh.dest_pincode),
                     dest_city            = COALESCE(s.dest_city,            sh.dest_city),
                     dest_state           = COALESCE(s.dest_state,           sh.dest_state),
                     current_location     = COALESCE(s.current_location,     sh.current_location),
                     updated_at           = now()
                FROM _stage_velocity_orders s
               WHERE sh.awb_number = s.awb_number
              RETURNING sh.awb_number
            )
            SELECT COUNT(*) FROM upd;
            """
        )
        updated = cur.fetchone()[0]

        cur.execute(
            """
            WITH ins AS (
              INSERT INTO shipments (
                awb_number, order_id, courier, actual_carrier, shipping_partner, aggregator,
                status, is_rto, shipped_at, manifested_at, delivered_at,
                rto_initiated_at, rto_delivered_at, estimated_delivery,
                shipping_cost, rto_cost, cod_amount,
                weight_charged, charged_weight, delivery_zone,
                ndr_count, ndr_reason, rto_reason, payment_mode,
                dest_pincode, dest_city, dest_state, current_location,
                brand_id, created_at, updated_at
              )
              SELECT s.awb_number, o.id, s.courier, s.actual_carrier, s.shipping_partner, s.aggregator,
                     s.status, s.is_rto, s.shipped_at, s.manifested_at, s.delivered_at,
                     s.rto_initiated_at, s.rto_delivered_at, s.estimated_delivery,
                     s.shipping_cost, s.rto_cost, s.cod_amount,
                     s.weight_charged, s.charged_weight, s.delivery_zone,
                     s.ndr_count, s.ndr_reason, s.rto_reason, s.payment_mode,
                     s.dest_pincode, s.dest_city, s.dest_state, s.current_location,
                     s.brand_id, now(), now()
                FROM _stage_velocity_orders s
                LEFT JOIN orders o ON o.order_number = s.order_number
               WHERE NOT EXISTS (
                 SELECT 1 FROM shipments sh WHERE sh.awb_number = s.awb_number
               )
              RETURNING 1
            )
            SELECT COUNT(*) FROM ins;
            """
        )
        inserted = cur.fetchone()[0]
    return {"updated": updated, "inserted": inserted}


def apply_cod_remit_detailed(conn, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TEMP TABLE _stage_cod_remit (
              awb_number text PRIMARY KEY,
              cod_remittance_amount numeric,
              cod_remittance_date timestamptz,
              cod_remitted boolean
            ) ON COMMIT DROP;
            """
        )
        with cur.copy(
            "COPY _stage_cod_remit (awb_number, cod_remittance_amount, cod_remittance_date, cod_remitted) FROM STDIN"
        ) as cp:
            for r in rows:
                cp.write_row(
                    [
                        r["awb_number"],
                        r["cod_remittance_amount"],
                        r["cod_remittance_date"],
                        r["cod_remitted"],
                    ]
                )
        cur.execute(
            """
            WITH upd AS (
              UPDATE shipments sh
                 SET cod_remittance_amount = r.cod_remittance_amount,
                     cod_remittance_date   = r.cod_remittance_date,
                     cod_remitted          = TRUE,
                     updated_at            = now()
                FROM _stage_cod_remit r
               WHERE sh.awb_number = r.awb_number
              RETURNING 1
            )
            SELECT COUNT(*) FROM upd;
            """
        )
        return cur.fetchone()[0]


def upsert_cod_settlements(conn, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO cod_remittances
              (aggregator, remittance_id, remittance_date, utr, cod_remitted, remittance_status, brand_id)
            VALUES (%(aggregator)s, %(remittance_id)s, %(remittance_date)s, %(utr)s,
                    %(cod_remitted)s, %(remittance_status)s, %(brand_id)s)
            ON CONFLICT (brand_id, remittance_id) DO UPDATE
               SET remittance_date    = EXCLUDED.remittance_date,
                   utr                = EXCLUDED.utr,
                   cod_remitted       = EXCLUDED.cod_remitted,
                   remittance_status  = EXCLUDED.remittance_status,
                   updated_at         = now();
            """,
            rows,
        )
        return len(rows)


def find_csv(folder: Path, needle: str) -> Path | None:
    matches = sorted(folder.glob(f"*{needle}*.csv"))
    return matches[-1] if matches else None  # newest by Velocity timestamp suffix


def main():
    ap = argparse.ArgumentParser(description="Backfill Velocity CSVs into Flanq Supabase.")
    ap.add_argument("folder", help="folder containing the 3 Velocity CSV exports")
    ap.add_argument("--dry-run", action="store_true", help="parse + run UPSERTs in a transaction, then ROLLBACK")
    args = ap.parse_args()

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        sys.exit(f"not a directory: {folder}")

    env = load_env(ENV_PATH)
    db_url = env.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit(
            "DATABASE_URL not found. Add it to scripts/.env, e.g.\n"
            "  DATABASE_URL=postgresql://postgres.<ref>:<pwd>@aws-0-<region>.pooler.supabase.com:5432/postgres\n"
            "Get it from Supabase dashboard -> Settings -> Database -> 'Connection string' -> URI."
        )

    order_csv = find_csv(folder, "Order Report")
    remit_csv = find_csv(folder, "COD Remittance Detailed Report")
    settle_csv = find_csv(folder, "COD Settlement Report")

    print(f"Folder: {folder}")
    print(f"  Order report       : {order_csv.name if order_csv else 'NOT FOUND'}")
    print(f"  COD remit detailed : {remit_csv.name if remit_csv else 'NOT FOUND'}")
    print(f"  COD settlement     : {settle_csv.name if settle_csv else 'NOT FOUND'}")
    if not order_csv:
        sys.exit("at minimum the Order Report is required.")

    order_rows = list(parse_order_report(order_csv))
    remit_rows = list(parse_cod_remit_detailed(remit_csv)) if remit_csv else []
    settle_rows = list(parse_cod_settlement(settle_csv)) if settle_csv else []
    print(
        f"\nParsed: {len(order_rows)} order rows, "
        f"{len(remit_rows)} cod-remit rows, {len(settle_rows)} settlement rows."
    )

    with db(db_url, dry_run=args.dry_run) as conn:
        before = snapshot(conn)
        print("\n--- BEFORE ---")
        for k, v in before.items():
            print(f"  {k:<22} {v}")

        staged = stage_order_report(conn, order_rows)
        print(f"\nStaged {staged} order rows.")
        result = upsert_shipments(conn)
        print(f"  shipments updated:  {result['updated']}")
        print(f"  shipments inserted: {result['inserted']}")

        if remit_rows:
            updated = apply_cod_remit_detailed(conn, remit_rows)
            print(f"  COD remit stamped on {updated} shipments")

        if settle_rows:
            n = upsert_cod_settlements(conn, settle_rows)
            print(f"  COD settlement rows upserted: {n}")

        after = snapshot(conn)
        print("\n--- AFTER ---")
        for k, v in after.items():
            delta = ""
            try:
                d = (v if isinstance(v, (int, float)) else float(v)) - (
                    before[k] if isinstance(before[k], (int, float)) else float(before[k])
                )
                if d:
                    delta = f"  ({'+' if d > 0 else ''}{d:g})"
            except (TypeError, ValueError):
                pass
            print(f"  {k:<22} {v}{delta}")


if __name__ == "__main__":
    main()
