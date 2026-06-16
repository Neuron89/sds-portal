import secrets
import sqlite3
import config

# Unambiguous slug alphabet — no 0/O/1/l/i to keep codes readable if a human
# ever has to type one off a label.
_SLUG_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'
_SLUG_LENGTH = 10


def get_db():
    db = sqlite3.connect(config.DATABASE)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    return db


def generate_slug(db):
    """Return a random slug that is not already used by any product.

    The slug is the permanent public identity of a product's QR code. It is
    decoupled from product_code/name so those can change forever without
    invalidating a printed label.
    """
    while True:
        slug = ''.join(secrets.choice(_SLUG_ALPHABET) for _ in range(_SLUG_LENGTH))
        exists = db.execute(
            'SELECT 1 FROM products WHERE qr_slug = ?', (slug,)
        ).fetchone()
        if not exists:
            return slug


def init_db():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_code TEXT UNIQUE NOT NULL,
            product_name TEXT NOT NULL,
            qr_slug TEXT,
            iqms_id INTEGER,
            eplant_id INTEGER,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sds_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            uploaded_by INTEGER NOT NULL,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (product_id) REFERENCES products(id),
            FOREIGN KEY (uploaded_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS access_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            product_id INTEGER NOT NULL,
            ip_address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0,
            FOREIGN KEY (product_id) REFERENCES products(id)
        );
    ''')
    db.commit()
    _migrate(db)
    db.close()


def _migrate(db):
    """Idempotent, in-place schema upgrades for existing databases."""
    cols = [r['name'] for r in db.execute('PRAGMA table_info(products)').fetchall()]

    # qr_slug: permanent QR identity, added after initial scaffold.
    if 'qr_slug' not in cols:
        db.execute('ALTER TABLE products ADD COLUMN qr_slug TEXT')

    # IQMS sync columns. iqms_id (ARINVT.ID) is the link key for items synced
    # from the ERP; NULL for manually-added products. ITEMNO/product_code is
    # NOT unique across plants, so never key on it for synced rows.
    if 'iqms_id' not in cols:
        db.execute('ALTER TABLE products ADD COLUMN iqms_id INTEGER')
    if 'eplant_id' not in cols:
        db.execute('ALTER TABLE products ADD COLUMN eplant_id INTEGER')
    if 'source' not in cols:
        db.execute("ALTER TABLE products ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    # Partial unique index so synced items can't double-insert, while manual
    # products (iqms_id IS NULL) are exempt.
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_products_iqms_id '
               'ON products(iqms_id) WHERE iqms_id IS NOT NULL')

    # Backfill any product missing a slug (covers both the ALTER above and any
    # rows inserted before slug generation existed).
    missing = db.execute(
        "SELECT id FROM products WHERE qr_slug IS NULL OR qr_slug = ''"
    ).fetchall()
    for row in missing:
        db.execute('UPDATE products SET qr_slug = ? WHERE id = ?',
                   (generate_slug(db), row['id']))

    # Enforce uniqueness going forward.
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_products_qr_slug '
               'ON products(qr_slug)')
    db.commit()
