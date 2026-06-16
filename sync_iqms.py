#!/usr/bin/env python3
"""Pull finished-good items from IQMS (DELMIAworks) into the SDS portal.

Read-only against IQMS: SELECTs ARINVT finished goods (CLASS='FG', sellable)
for the configured plant and upserts them into the local `products` table,
keyed on the IQMS surrogate ID (ARINVT.ID) — never ITEMNO, which is not unique.

Usage:
    python3 sync_iqms.py            # apply
    python3 sync_iqms.py --dry-run  # report what would change, write nothing
"""
import os
import sys
import sqlite3
from pathlib import Path

# Load .env the same way app.py does (systemd/shell env still wins).
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
from db import get_db, generate_slug

FG_QUERY = """
    SELECT ID, ITEMNO, DESCRIP, EPLANT_ID
    FROM IQMS.ARINVT
    WHERE CLASS = 'FG'
      AND NVL(NON_SALABLE, 'N') <> 'Y'
      AND EPLANT_ID = :plant
    ORDER BY ITEMNO
"""


def fetch_finished_goods(plant_id):
    if not config.IQMS_DB_USER or not config.IQMS_DB_PASSWORD:
        sys.exit("IQMS_DB_USER / IQMS_DB_PASSWORD not set in environment.")
    conn = oracledb.connect(
        user=config.IQMS_DB_USER,
        password=config.IQMS_DB_PASSWORD,
        dsn=config.IQMS_DB_DSN,
        config_dir=config.IQMS_TNS_ADMIN,
    )
    try:
        cur = conn.cursor()
        cur.execute(FG_QUERY, plant=plant_id)
        return cur.fetchall()
    finally:
        conn.close()


def sync(dry_run=False):
    plant = config.IQMS_EPLANT_ID
    rows = fetch_finished_goods(plant)
    db = get_db()
    inserted = updated = skipped = 0
    for arinvt_id, itemno, descrip, eplant in rows:
        itemno = (itemno or "").strip()
        descrip = (descrip or "").strip() or itemno
        if not itemno:
            skipped += 1
            continue
        existing = db.execute(
            "SELECT id FROM products WHERE iqms_id = ?", (arinvt_id,)
        ).fetchone()
        try:
            if existing:
                if not dry_run:
                    db.execute(
                        "UPDATE products SET product_code = ?, product_name = ?, "
                        "eplant_id = ?, updated_at = CURRENT_TIMESTAMP WHERE iqms_id = ?",
                        (itemno, descrip, eplant, arinvt_id),
                    )
                updated += 1
            else:
                # Don't clobber a manually-created product that happens to share
                # this code; flag it for a human instead.
                clash = db.execute(
                    "SELECT id FROM products WHERE product_code = ? AND iqms_id IS NULL",
                    (itemno,),
                ).fetchone()
                if clash:
                    skipped += 1
                    print(f"SKIP {itemno}: code already used by a manual product")
                    continue
                if not dry_run:
                    db.execute(
                        "INSERT INTO products (product_code, product_name, qr_slug, "
                        "iqms_id, eplant_id, source) VALUES (?, ?, ?, ?, ?, 'iqms')",
                        (itemno, descrip, generate_slug(db), arinvt_id, eplant),
                    )
                inserted += 1
        except sqlite3.IntegrityError as e:
            skipped += 1
            print(f"SKIP {itemno} (iqms_id={arinvt_id}): {e}")

    if not dry_run:
        db.commit()
    db.close()
    tag = " (DRY RUN — nothing written)" if dry_run else ""
    print(f"IQMS sync plant={plant}: {len(rows)} FG items | "
          f"inserted={inserted} updated={updated} skipped={skipped}{tag}")


if __name__ == "__main__":
    sync(dry_run="--dry-run" in sys.argv)
