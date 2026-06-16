import os
import uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

# Lightweight .env loader (python-dotenv not always installed). Only sets
# vars that aren't already in os.environ, so systemd / shell wins.
_env_path = Path(__file__).parent / ".env"
if _env_path.is_file():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, abort, session, make_response
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import jwt

import config
from db import get_db, init_db

app = Flask(__name__)
app.config['SECRET_KEY'] = config.SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = config.MAX_CONTENT_LENGTH


@app.context_processor
def inject_portal_chrome():
    """PORTAL_URL + CLARITY_PROJECT_ID exposed to base.html."""
    return {
        'PORTAL_URL': os.environ.get('PORTAL_URL', ''),
        'CLARITY_PROJECT_ID': os.environ.get('CLARITY_PROJECT_ID', ''),
        'PUBLIC_BASE_URL': config.PUBLIC_BASE_URL,
    }


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'


# --- User model for Flask-Login ---

class User(UserMixin):
    def __init__(self, id, username):
        self.id = id
        self.username = username


@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    row = db.execute('SELECT id, username FROM users WHERE id = ?', (user_id,)).fetchone()
    db.close()
    if row:
        return User(row['id'], row['username'])
    return None


# --- Token helpers ---

def generate_access_token(product_id, ip_address):
    """Create a short-lived, IP-bound access token."""
    token = uuid.uuid4().hex
    expires_at = datetime.utcnow() + timedelta(minutes=config.TOKEN_EXPIRY_MINUTES)
    db = get_db()
    db.execute(
        'INSERT INTO access_tokens (token, product_id, ip_address, expires_at, used) '
        'VALUES (?, ?, ?, ?, 1)',
        (token, product_id, ip_address, expires_at)
    )
    db.commit()
    db.close()
    return token


def validate_access_token(token, product_id, ip_address):
    """Check if token is valid, not expired, and matches IP."""
    db = get_db()
    row = db.execute(
        'SELECT * FROM access_tokens WHERE token = ? AND product_id = ? AND ip_address = ?',
        (token, product_id, ip_address)
    ).fetchone()
    db.close()
    if not row:
        return False
    expires_at = datetime.fromisoformat(row['expires_at'])
    return datetime.utcnow() < expires_at


def cleanup_expired_tokens():
    """Remove expired tokens."""
    db = get_db()
    db.execute('DELETE FROM access_tokens WHERE expires_at < ?', (datetime.utcnow(),))
    db.commit()
    db.close()


# --- Public routes (QR code entry point) ---

def lookup_product(db, ref):
    """Resolve a public reference to a product. Prefers the permanent qr_slug
    (what QR labels encode); falls back to product_code for convenience and
    backward compatibility with any links created before slugs existed."""
    product = db.execute(
        'SELECT * FROM products WHERE qr_slug = ?', (ref,)
    ).fetchone()
    if not product:
        product = db.execute(
            'SELECT * FROM products WHERE product_code = ?', (ref,)
        ).fetchone()
    return product


def active_sds_for(db, product_id):
    return db.execute(
        'SELECT * FROM sds_files WHERE product_id = ? AND is_active = 1 '
        'ORDER BY uploaded_at DESC LIMIT 1', (product_id,)
    ).fetchone()


@app.route('/')
def root():
    """Root entry. Authenticated employees go to the dashboard. The public is
    shown a neutral dead-end (no link into the system) — they should only ever
    arrive via a QR code that lands on /sds/<slug>."""
    if current_user.is_authenticated:
        return redirect(url_for('admin_dashboard'))
    return render_template('error.html',
                           title='NYCOA Safety Data Sheets',
                           message='Scan the QR code on the product label to view its '
                                   'Safety Data Sheet.'), 200


@app.route('/sds/<ref>')
def view_sds(ref):
    """QR code landing page. Generates a token and shows the PDF viewer."""
    db = get_db()
    product = lookup_product(db, ref)
    if not product:
        db.close()
        abort(404)
    sds = active_sds_for(db, product['id'])

    # Admins bypass token checks
    if current_user.is_authenticated:
        db.close()
        if not sds:
            abort(404)
        return render_template('view_sds.html', product=product, sds=sds,
                               token='admin', ref=ref)

    db.close()
    if not sds:
        abort(404)

    ip = request.remote_addr
    # Reuse a still-valid token from this session, else mint a fresh IP-bound one.
    session_token = session.get(f'sds_token_{ref}')
    if session_token and validate_access_token(session_token, product['id'], ip):
        token = session_token
    else:
        token = generate_access_token(product['id'], ip)
        session[f'sds_token_{ref}'] = token

    return render_template('view_sds.html', product=product, sds=sds,
                           token=token, ref=ref)


def send_pdf_readonly(filepath):
    """Send a PDF with headers that prevent downloading/caching."""
    response = make_response(send_file(filepath, mimetype='application/pdf'))
    response.headers['Content-Disposition'] = 'inline'
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.route('/sds/<ref>/pdf')
def serve_pdf(ref):
    """Serve the actual PDF file. Requires valid token or admin login."""
    db = get_db()
    product = lookup_product(db, ref)
    if not product:
        db.close()
        abort(404)

    if not current_user.is_authenticated:
        token = request.args.get('token')
        if not token or not validate_access_token(token, product['id'], request.remote_addr):
            db.close()
            abort(403)

    sds = active_sds_for(db, product['id'])
    db.close()
    if not sds:
        abort(404)

    filepath = os.path.join(config.UPLOAD_FOLDER, sds['filename'])
    if not os.path.exists(filepath):
        abort(404)

    return send_pdf_readonly(filepath)


# --- Error pages ---

@app.errorhandler(403)
def forbidden(e):
    return render_template('error.html',
                           title='Access Denied',
                           message='This link has expired or is not valid. '
                                   'Please scan the QR code on the product label to access the Safety Data Sheet.'), 403


@app.errorhandler(404)
def not_found(e):
    return render_template('error.html',
                           title='Not Found',
                           message='The requested Safety Data Sheet could not be found.'), 404


# --- Auth routes ---

@app.route('/admin/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        db = get_db()
        row = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()
        if row and check_password_hash(row['password_hash'], password):
            user = User(row['id'], row['username'])
            login_user(user)
            return redirect(url_for('admin_dashboard'))
        flash('Invalid username or password.', 'error')
    return render_template('login.html')


@app.route('/admin/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/sso')
def sso():
    """Single sign-on entry from the NYCOA Portal. The portal mints a 5-minute
    HS256 JWT (signed with PORTAL_SSO_SECRET, aud='sds', iss='nycoa-portal') and
    redirects here as /sso?ptoken=<jwt>&next=<path>. A valid token means the
    portal already authorized this employee for SDS, so we provision/look up a
    local user by email and log them in."""
    if not config.PORTAL_SSO_SECRET:
        abort(503)
    ptoken = request.args.get('ptoken', '')
    if not ptoken:
        abort(403)
    try:
        claims = jwt.decode(
            ptoken, config.PORTAL_SSO_SECRET, algorithms=['HS256'],
            audience=config.SSO_AUDIENCE, issuer=config.SSO_ISSUER,
        )
    except jwt.InvalidTokenError:
        flash('That sign-on link is invalid or has expired. Please open SDS Portal '
              'from the NYCOA Portal again.', 'error')
        return redirect(url_for('login'))

    email = (claims.get('email') or '').strip().lower()
    if not email:
        abort(403)

    db = get_db()
    row = db.execute('SELECT id, username FROM users WHERE username = ?', (email,)).fetchone()
    if not row:
        # First SSO arrival — provision a local account with an unusable
        # password (login is only ever via the portal for these users).
        db.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
                   (email, generate_password_hash(uuid.uuid4().hex)))
        db.commit()
        row = db.execute('SELECT id, username FROM users WHERE username = ?', (email,)).fetchone()
    db.close()

    login_user(User(row['id'], row['username']))

    # Only follow a same-origin relative path; never an absolute/external URL.
    nxt = request.args.get('next', '')
    if nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return redirect(url_for('admin_dashboard'))


# --- Admin routes ---

@app.route('/admin')
@login_required
def admin_dashboard():
    db = get_db()
    products = db.execute(
        'SELECT p.*, sf.original_filename, sf.uploaded_at as sds_uploaded_at, u.username as uploaded_by_name '
        'FROM products p '
        'LEFT JOIN sds_files sf ON sf.product_id = p.id AND sf.is_active = 1 '
        'LEFT JOIN users u ON sf.uploaded_by = u.id '
        'ORDER BY p.product_code'
    ).fetchall()
    db.close()
    return render_template('admin_dashboard.html', products=products)


@app.route('/admin/product/<int:product_id>')
@login_required
def product_detail(product_id):
    """Per-product workspace: SDS upload, version history, and the QR/label
    URL for this product."""
    db = get_db()
    product = db.execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()
    if not product:
        db.close()
        abort(404)
    history = db.execute(
        'SELECT sf.*, u.username AS uploaded_by_name '
        'FROM sds_files sf LEFT JOIN users u ON sf.uploaded_by = u.id '
        'WHERE sf.product_id = ? ORDER BY sf.uploaded_at DESC',
        (product_id,)
    ).fetchall()
    db.close()
    public_url = f"{config.PUBLIC_BASE_URL}/sds/{product['qr_slug']}"
    current_sds = next((h for h in history if h['is_active']), None)
    return render_template('product_detail.html', product=product, history=history,
                           current_sds=current_sds, public_url=public_url)


@app.route('/admin/product/add', methods=['GET', 'POST'])
@login_required
def add_product():
    if request.method == 'POST':
        product_code = request.form.get('product_code', '').strip().upper()
        product_name = request.form.get('product_name', '').strip()
        if not product_code or not product_name:
            flash('Product code and name are required.', 'error')
            return render_template('add_product.html')
        db = get_db()
        existing = db.execute('SELECT id FROM products WHERE product_code = ?', (product_code,)).fetchone()
        if existing:
            db.close()
            flash('A product with that code already exists.', 'error')
            return render_template('add_product.html')
        from db import generate_slug
        slug = generate_slug(db)
        cur = db.execute(
            'INSERT INTO products (product_code, product_name, qr_slug) VALUES (?, ?, ?)',
            (product_code, product_name, slug))
        product_id = cur.lastrowid
        db.commit()
        db.close()
        flash(f'Product {product_code} added.', 'success')
        # Land on the product workspace so they can upload the SDS + grab the QR.
        return redirect(url_for('product_detail', product_id=product_id))
    return render_template('add_product.html')


@app.route('/admin/product/<int:product_id>/upload', methods=['GET', 'POST'])
@login_required
def upload_sds(product_id):
    db = get_db()
    product = db.execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()
    if not product:
        db.close()
        abort(404)

    if request.method == 'POST':
        file = request.files.get('sds_file')
        if not file or file.filename == '':
            db.close()
            flash('No file selected.', 'error')
            return redirect(url_for('product_detail', product_id=product_id))

        if not file.filename.lower().endswith('.pdf'):
            db.close()
            flash('Only PDF files are allowed.', 'error')
            return redirect(url_for('product_detail', product_id=product_id))

        # Deactivate old SDS files for this product
        db.execute('UPDATE sds_files SET is_active = 0 WHERE product_id = ?', (product_id,))

        # Save new file
        original_filename = secure_filename(file.filename)
        stored_filename = f"{product['product_code']}_{uuid.uuid4().hex[:8]}.pdf"
        filepath = os.path.join(config.UPLOAD_FOLDER, stored_filename)
        file.save(filepath)

        db.execute(
            'INSERT INTO sds_files (product_id, filename, original_filename, uploaded_by) '
            'VALUES (?, ?, ?, ?)',
            (product_id, stored_filename, original_filename, current_user.id)
        )
        db.execute('UPDATE products SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (product_id,))
        db.commit()
        db.close()
        flash(f'SDS uploaded for {product["product_code"]}.', 'success')
        return redirect(url_for('product_detail', product_id=product_id))

    # The upload form lives on the product workspace page now.
    db.close()
    return redirect(url_for('product_detail', product_id=product_id))


@app.route('/admin/product/<int:product_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_product(product_id):
    db = get_db()
    product = db.execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()
    if not product:
        db.close()
        abort(404)
    if request.method == 'POST':
        product_code = request.form.get('product_code', '').strip().upper()
        product_name = request.form.get('product_name', '').strip()
        if not product_code or not product_name:
            flash('Product code and name are required.', 'error')
            return render_template('edit_product.html', product=product)
        existing = db.execute(
            'SELECT id FROM products WHERE product_code = ? AND id != ?',
            (product_code, product_id)
        ).fetchone()
        if existing:
            db.close()
            flash('Another product with that code already exists.', 'error')
            return render_template('edit_product.html', product=product)
        db.execute(
            'UPDATE products SET product_code = ?, product_name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            (product_code, product_name, product_id)
        )
        db.commit()
        db.close()
        flash('Product updated.', 'success')
        return redirect(url_for('admin_dashboard'))
    db.close()
    return render_template('edit_product.html', product=product)


@app.route('/admin/product/<int:product_id>/delete', methods=['POST'])
@login_required
def delete_product(product_id):
    db = get_db()
    product = db.execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()
    if not product:
        db.close()
        abort(404)
    # Delete associated files from disk
    files = db.execute('SELECT filename FROM sds_files WHERE product_id = ?', (product_id,)).fetchall()
    for f in files:
        filepath = os.path.join(config.UPLOAD_FOLDER, f['filename'])
        if os.path.exists(filepath):
            os.remove(filepath)
    db.execute('DELETE FROM sds_files WHERE product_id = ?', (product_id,))
    db.execute('DELETE FROM access_tokens WHERE product_id = ?', (product_id,))
    db.execute('DELETE FROM products WHERE id = ?', (product_id,))
    db.commit()
    db.close()
    flash(f'Product {product["product_code"]} deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/users')
@login_required
def manage_users():
    db = get_db()
    users = db.execute('SELECT id, username, created_at FROM users ORDER BY username').fetchall()
    db.close()
    return render_template('manage_users.html', users=users)


@app.route('/admin/users/add', methods=['GET', 'POST'])
@login_required
def add_user():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            flash('Username and password are required.', 'error')
            return render_template('add_user.html')
        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'error')
            return render_template('add_user.html')
        db = get_db()
        existing = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
        if existing:
            db.close()
            flash('Username already exists.', 'error')
            return render_template('add_user.html')
        db.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
                   (username, generate_password_hash(password)))
        db.commit()
        db.close()
        flash(f'User {username} created.', 'success')
        return redirect(url_for('manage_users'))
    return render_template('add_user.html')


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@login_required
def delete_user(user_id):
    if user_id == current_user.id:
        flash('You cannot delete your own account.', 'error')
        return redirect(url_for('manage_users'))
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    db.close()
    flash('User deleted.', 'success')
    return redirect(url_for('manage_users'))


# --- CLI command to create initial admin ---

def create_admin(username, password):
    db = get_db()
    existing = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    if existing:
        print(f'User "{username}" already exists.')
        db.close()
        return
    db.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
               (username, generate_password_hash(password)))
    db.commit()
    db.close()
    print(f'Admin user "{username}" created.')


# --- Periodic cleanup ---

# Endpoints reachable on the public (QR) host. Everything else is the
# employee system and is hidden behind the admin host.
_PUBLIC_ENDPOINTS = {'root', 'view_sds', 'serve_pdf', 'static'}


@app.before_request
def enforce_surface_separation():
    """When an admin host is configured, the public host serves only the
    read-only viewer — admin/login/SSO routes 404 there, so a QR scanner has
    no path back into the system."""
    if not config.SDS_ADMIN_HOST:
        return  # single-host mode, no enforcement yet
    host = request.host.split(':')[0].lower()
    if host == config.SDS_PUBLIC_HOST and request.endpoint not in _PUBLIC_ENDPOINTS:
        abort(404)


@app.before_request
def cleanup_tokens_occasionally():
    # Clean up expired tokens ~1% of requests
    import random
    if random.random() < 0.01:
        cleanup_expired_tokens()


# --- Init ---

if __name__ == '__main__':
    import sys
    os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
    init_db()

    if len(sys.argv) > 1 and sys.argv[1] == 'create-admin':
        if len(sys.argv) != 4:
            print('Usage: python app.py create-admin <username> <password>')
            sys.exit(1)
        create_admin(sys.argv[2], sys.argv[3])
        sys.exit(0)

    app.run(host='0.0.0.0', port=5001, debug=True)
