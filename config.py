import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

SECRET_KEY = os.environ.get('SECRET_KEY', 'change-me-in-production')
DATABASE = os.path.join(BASE_DIR, 'sds_portal.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB max upload

# Access token settings
TOKEN_EXPIRY_MINUTES = 10
