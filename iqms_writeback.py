#!/usr/bin/env python3
"""Write each product's permanent SDS URL back into IQMS ARINVT (CUSER9).

This is the ONLY thing that writes to the ERP, and it touches exactly one
column (config.IQMS_URL_FIELD) on rows matched by the IQMS surrogate ID.
Uses a SEPARATE write account (IQMS_WRITE_USER/PASSWORD) so the read-only
sync account can never write.

SAFETY: dry-run by default. Nothing is written unless you pass --commit.

Usage:
    python3 iqms_writeback.py --code 10300            # preview one item
    python3 iqms_writeback.py --code 10300 --commit   # write that one item
    python3 iqms_writeback.py --all                   # preview all eligible
    python3 iqms_writeback.py --all --commit          # write all eligible

Eligible = products synced from IQMS (iqms_id NOT NULL). Add --require-sds to
only write items that already have an uploaded SDS (so a scanned label never
lands on a 'not found' page).
"""
import os
import re
import sys
import argparse
from pathlib import Path

# Load .env like app.py.
_env_path = Path(__file__).parent / ".env"
if _env_path.is_file():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import oracledb

import config
from db import get_db

# Whitelist the target column — it is interpolated into SQL (Oracle can't bind
# identifiers), so it must never come from untrusted input.
if not re.fullmatch(r"CUSER([1-9]|10)", config.IQMS_URL_FIELD or ""):
    sys.exit(f"Refusing to run: IQMS_URL_FIELD={config.IQMS_URL_FIELD!r} is not a valid CUSER field.")
FIELD = config.IQMS_URL_FIELD


def eligible_products(code=None, require_sds=False):
    db = get_db()
    sql = ("SELECT p.id, p.product_code, p.qr_slug, p.iqms_id, "
           "(SELECT COUNT(*) FROM sds_files sf WHERE sf.product_id = p.id AND sf.is_active = 1) AS has_sds "
           "FROM products p WHERE p.iqms_id IS NOT NULL AND p.qr_slug IS NOT NULL")
    params = []
    if code:
        sql += " AND p.product_code = ?"
        params.append(code)
    rows = db.execute(sql, params).fetchall()
    db.close()
    out = []
    for r in rows:
        if require_sds and not r["has_sds"]:
            continue
        out.append(r)
    return out


def connect_write():
    user = config.IQMS_WRITE_USER
    pw = config.IQMS_WRITE_PASSWORD
    if not user or not pw:
        sys.exit("IQMS_WRITE_USER / IQMS_WRITE_PASSWORD not set — provision the write account first.")
    return oracledb.connect(user=user, password=pw, dsn=config.IQMS_DB_DSN,
                            config_dir=config.IQMS_TNS_ADMIN)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--code", help="single product_code (ITEMNO) to write")
    g.add_argument("--all", action="store_true", help="all eligible synced items")
    ap.add_argument("--commit", action="store_true", help="actually write (default: dry-run)")
    ap.add_argument("--require-sds", action="store_true", help="only items with an uploaded SDS")
    args = ap.parse_args()

    items = eligible_products(code=args.code, require_sds=args.require_sds)
    if not items:
        sys.exit("No matching eligible products.")

    conn = connect_write()
    cur = conn.cursor()
    sel = f"SELECT {FIELD} FROM IQMS.ARINVT WHERE ID = :id"
    upd = f"UPDATE IQMS.ARINVT SET {FIELD} = :url WHERE ID = :id"

    changed = 0
    for it in items:
        url = f"{config.PUBLIC_BASE_URL}/sds/{it['qr_slug']}"
        cur.execute(sel, id=it["iqms_id"])
        before = cur.fetchone()
        before = before[0] if before else "<no such ARINVT row>"
        if before == url:
            print(f"= {it['product_code']} (ID {it['iqms_id']}): already set, skipping")
            continue
        print(f"{'WRITE' if args.commit else 'WOULD WRITE'} {it['product_code']} (ID {it['iqms_id']}): "
              f"{FIELD} {before!r} -> {url!r}")
        if args.commit:
            cur.execute(upd, url=url, id=it["iqms_id"])
            changed += 1

    if args.commit:
        conn.commit()
        print(f"Committed {changed} update(s) to IQMS.ARINVT.{FIELD}.")
    else:
        print(f"DRY RUN — nothing written. {len(items)} item(s) inspected. Re-run with --commit to apply.")
    conn.close()


if __name__ == "__main__":
    main()
