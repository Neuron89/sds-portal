import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

SECRET_KEY = os.environ.get('SECRET_KEY', 'change-me-in-production')
DATABASE = os.path.join(BASE_DIR, 'sds_portal.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB max upload

# Access token settings
TOKEN_EXPIRY_MINUTES = 10

# Public base URL customers reach (through the Cloudflare tunnel). Used to build
# the canonical QR/label URL for each product. Override via env in .env.
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'https://sds.nycoa.io').rstrip('/')

# Single sign-on from the NYCOA Portal. The portal mints a short-lived HS256
# JWT signed with this shared secret; we verify it and pin issuer/audience.
PORTAL_SSO_SECRET = os.environ.get('PORTAL_SSO_SECRET')
SSO_ISSUER = 'nycoa-portal'
SSO_AUDIENCE = 'sds'

# Surface separation. The public host (where QR codes point) serves ONLY the
# read-only SDS viewer — no login, admin, or SSO routes — so a customer who
# scans a label has no path back into the employee system. When SDS_ADMIN_HOST
# is set, this separation is enforced by host; until then the app runs in
# single-host mode (everything served on one host) to avoid lockout.
SDS_PUBLIC_HOST = os.environ.get('SDS_PUBLIC_HOST', 'sds.nycoa.io').split(':')[0].lower()
SDS_ADMIN_HOST = (os.environ.get('SDS_ADMIN_HOST', '') or '').split(':')[0].lower()

# --- IQMS (DELMIAworks) ERP integration ---
# Read sync uses the shared read-only account; write-back (CUSER9) uses a
# separate, narrowly-scoped account so the portal can never do more than
# intended. TNS alias IQORA survives Oracle IP cutovers.
IQMS_DB_USER = os.environ.get('IQMS_DB_USER')
IQMS_DB_PASSWORD = os.environ.get('IQMS_DB_PASSWORD')
IQMS_DB_DSN = os.environ.get('IQMS_DB_DSN', 'IQORA')
IQMS_TNS_ADMIN = os.environ.get('TNS_ADMIN', '/usr/lib/oracle/19.28/client64/lib/network/admin')
# Which plant's finished goods to sync (2 = NYCOA). FG = ARINVT.CLASS 'FG'.
IQMS_EPLANT_ID = int(os.environ.get('IQMS_EPLANT_ID', '2'))
# ARINVT character user-field that holds the SDS URL (NUSER1 is numeric).
IQMS_URL_FIELD = os.environ.get('IQMS_URL_FIELD', 'CUSER9')

# Write-back account (Phase 2). Falls back to read-only creds only if you
# explicitly point it there; keep separate in production.
IQMS_WRITE_USER = os.environ.get('IQMS_WRITE_USER')
IQMS_WRITE_PASSWORD = os.environ.get('IQMS_WRITE_PASSWORD')
