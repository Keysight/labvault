import os
from pathlib import Path

# Load .env file when present (development convenience)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent

# ============================================================
# SECURITY — override every value via environment variables
# in production. See .env.example.
# ============================================================
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY') or os.environ.get('SECRET_KEY') or ''

DEBUG = os.environ.get('DJANGO_DEBUG', 'False').lower() in ('1', 'true', 'yes')

_allowed = os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1')
ALLOWED_HOSTS = [h.strip() for h in _allowed.split(',') if h.strip()]
if '*' in ALLOWED_HOSTS:
    raise RuntimeError('DJANGO_ALLOWED_HOSTS must not contain * on customer SKU')

# TLS is the customer default. Nginx terminates HTTPS on LABVAULT_TLS_PORT
# (9443 — unused elsewhere in this SKU). Gunicorn stays on loopback :8000.
# Set LABVAULT_USE_TLS=false only for local HTTP debugging.
_use_tls = os.environ.get('LABVAULT_USE_TLS', 'true').lower() in ('1', 'true', 'yes')
try:
    LABVAULT_TLS_PORT = int(os.environ.get('LABVAULT_TLS_PORT', '9443') or '9443')
except ValueError:
    LABVAULT_TLS_PORT = 9443
# Loopback probes may still hit gunicorn over plain HTTP on :8000.
_allow_local_http_api = os.environ.get('LABVAULT_ALLOW_LOCAL_HTTP_API', 'true').lower() in (
    '1', 'true', 'yes',
)
if _use_tls:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = not _allow_local_http_api
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    _hsts = os.environ.get('LABVAULT_HSTS_SECONDS', '0').strip()
    if _hsts.isdigit() and int(_hsts) > 0:
        SECURE_HSTS_SECONDS = int(_hsts)
        SECURE_HSTS_INCLUDE_SUBDOMAINS = True

def _https_origin(host: str, port: int) -> str:
    host = (host or '').strip()
    if not host:
        return ''
    if port in (443, 0):
        return f'https://{host}'
    return f'https://{host}:{port}'


# CSRF trusted origins (Django 4+ Origin check). Default HTTPS :9443.
# Override with LABVAULT_CSRF_TRUSTED_ORIGINS when using a custom host/port.
_csrf_origins = os.environ.get('LABVAULT_CSRF_TRUSTED_ORIGINS', '').strip()
if _csrf_origins:
    CSRF_TRUSTED_ORIGINS = [
        o.strip() for o in _csrf_origins.split(',') if o.strip()
    ]
elif _use_tls:
    _public_host = os.environ.get('LABVAULT_PUBLIC_HOSTNAME', '').strip()
    CSRF_TRUSTED_ORIGINS = [
        origin
        for origin in (
            _https_origin(_public_host, LABVAULT_TLS_PORT) if _public_host else '',
            _https_origin('127.0.0.1', LABVAULT_TLS_PORT),
            _https_origin('localhost', LABVAULT_TLS_PORT),
        )
        if origin
    ]


# Session idle timeout (default 1 hour). Extend on each request when SAVE_EVERY_REQUEST is true.
SESSION_COOKIE_AGE = int(os.environ.get('LABVAULT_SESSION_COOKIE_AGE', '3600'))
SESSION_SAVE_EVERY_REQUEST = os.environ.get(
    'LABVAULT_SESSION_SAVE_EVERY_REQUEST', 'true',
).lower() in ('1', 'true', 'yes')
SESSION_EXPIRE_AT_BROWSER_CLOSE = os.environ.get(
    'LABVAULT_SESSION_EXPIRE_AT_BROWSER_CLOSE', 'false',
).lower() in ('1', 'true', 'yes')

# Shared cache across gunicorn workers (Keysight chassis data, node associations, etc.)
def _ensure_writable_dir(preferred: str, fallback: Path) -> Path:
    path = Path(preferred)
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o775)
        return path
    except OSError:
        fallback.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(fallback, 0o775)
        except OSError:
            pass
        return fallback


_cache_dir_path = _ensure_writable_dir(
    os.environ.get('LABVAULT_CACHE_DIR', str(BASE_DIR / 'var' / 'django_cache')),
    BASE_DIR / 'var' / 'django_cache',
)
_cache_dir = str(_cache_dir_path)

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
        'LOCATION': _cache_dir,
        'OPTIONS': {'MAX_ENTRIES': 20000},
    },
}

# ============================================================
# Application definition
# ============================================================
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'sslserver',
    'connect',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'connect.middleware.RequestLogMiddleware',
]

ROOT_URLCONF = 'labvault.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'connect.context_processors.global_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'labvault.wsgi.application'

# ============================================================
# Database
# Defaults to SQLite for development.
# Set DATABASE_URL / NP_TIMESERIES_DATABASE_URL for PostgreSQL in production.
# ============================================================
def _database_from_url(env_name: str, default_sqlite_path: Path) -> dict:
    url = os.environ.get(env_name, '')
    if url.startswith('postgres'):
        import dj_database_url  # pip install dj-database-url psycopg2-binary
        # parse() the URL we already read. config(default=url) still prefers
        # DATABASE_URL, so np_timeseries would silently share the inventory DB.
        cfg = dj_database_url.parse(url)
        # Close connections after each request/work unit to avoid pool exhaustion
        # when background threads probe devices/chassis in parallel.
        cfg.setdefault('CONN_MAX_AGE', 0)
        cfg.setdefault('CONN_HEALTH_CHECKS', True)
        return cfg
    name = default_sqlite_path
    if url.startswith('sqlite'):
        # Honor sqlite:///rel or sqlite:////abs paths (used to place np_timeseries
        # on a shared/persistent volume). Normalize 3- and 4-slash forms to absolute.
        raw = url.split('://', 1)[1] if '://' in url else ''
        if raw:
            name = '/' + raw.lstrip('/')
    return {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': name,
        'OPTIONS': {'timeout': 30},
    }


DATABASES = {
    'default': _database_from_url('DATABASE_URL', BASE_DIR / 'db.sqlite3'),
    'np_timeseries': _database_from_url('NP_TIMESERIES_DATABASE_URL', BASE_DIR / 'np_timeseries.sqlite3'),
}
DATABASE_ROUTERS = [
    'connect.db_routers.NPTimeseriesRouter',
]

# ============================================================
# Static / Media files
# ============================================================
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATIC_URL = '/static/'

STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'connect/static'),
]

MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
MEDIA_URL = '/media/'

# Allow large file uploads for .enc.tar packages (up to 5 GB)
DATA_UPLOAD_MAX_MEMORY_SIZE = 5368709120   # 5 GB
FILE_UPLOAD_MAX_MEMORY_SIZE = 52428800     # 50 MB — files larger than this go to disk

# ============================================================
# Auth
# ============================================================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LOGIN_REDIRECT_URL = '/dashboard/'
LOGIN_URL = '/login/'

# Usernames (case-insensitive) that cannot change password via LabVault UI or Django admin User edit.
# Customer SKU default is empty so bootstrap admin can rotate labvault! after first login.
# Shared-account lock example: LABVAULT_PASSWORD_LOCKED_USERNAMES="admin,servicebot"
_pw_locked_raw = os.environ.get(
    'LABVAULT_PASSWORD_LOCKED_USERNAMES',
    '',
).strip()
LABVAULT_PASSWORD_LOCKED_USERNAMES = frozenset(
    x.strip().lower() for x in _pw_locked_raw.replace(',', ' ').split() if x.strip()
)

# Superusers who may set passwords for LABVAULT_PASSWORD_LOCKED_USERNAMES via Django admin (break-glass).
# Default: godmode. Override: LABVAULT_PASSWORD_BREAKGLASS_USERNAMES="godmode,backup"
_bg_raw = os.environ.get('LABVAULT_PASSWORD_BREAKGLASS_USERNAMES', 'godmode').strip()
LABVAULT_PASSWORD_BREAKGLASS_USERNAMES = frozenset(
    x.strip().lower() for x in _bg_raw.replace(',', ' ').split() if x.strip()
)

# Usernames break-glass accounts may reset from LabVault (plus any superuser who is not break-glass).
_br_targets_raw = os.environ.get(
    'LABVAULT_BREAKGLASS_RESETTABLE_USERNAMES',
    'admin mgmt finance',
).strip()
LABVAULT_BREAKGLASS_RESETTABLE_USERNAMES = frozenset(
    x.strip().lower() for x in _br_targets_raw.replace(',', ' ').split() if x.strip()
)

# Usernames allowed to use Django /admin/ (staff alone is not enough).
# Default: godmode only. mgmt/finance keep is_staff for in-app features (e.g. Hyperview).
_django_admin_raw = os.environ.get('LABVAULT_DJANGO_ADMIN_USERNAMES', 'godmode').strip()
LABVAULT_DJANGO_ADMIN_USERNAMES = frozenset(
    x.strip().lower() for x in _django_admin_raw.replace(',', ' ').split() if x.strip()
)

# ============================================================
# Internationalisation
# ============================================================
LANGUAGE_CODE = 'en-us'
TIME_ZONE = os.environ.get('TZ', 'UTC')
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ============================================================
# Logging
# ============================================================
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'diagnostics_ring': {
            'class': 'connect.log_ring.RingBufferHandler',
            'formatter': 'verbose',
            'level': 'WARNING',
        },
    },
    'root': {
        'handlers': ['console', 'diagnostics_ring'],
        'level': os.environ.get('DJANGO_LOG_LEVEL', 'INFO'),
    },
}


# ============================================================
# Email
# ============================================================
EMAIL_BACKEND = os.environ.get(
    'EMAIL_BACKEND',
    'django.core.mail.backends.console.EmailBackend',
)
EMAIL_HOST     = os.environ.get('EMAIL_HOST', 'localhost')
EMAIL_PORT     = int(os.environ.get('EMAIL_PORT', '25'))
EMAIL_USE_TLS  = os.environ.get('EMAIL_USE_TLS', 'False').lower() in ('1', 'true', 'yes')
EMAIL_HOST_USER     = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL  = os.environ.get('DEFAULT_FROM_EMAIL', 'labvault@localhost')

# ============================================================
# ============================================================
# Gunicorn runs as user labvault — cannot traverse /root; keep a synced copy here.

# ============================================================
# Keysight reservation notifications
# ============================================================
# ============================================================
# BMC Board — direct IPMI access
# ============================================================
BMC_DEFAULT_USER = os.environ.get('BMC_DEFAULT_USER', 'admin')
BMC_DEFAULT_PASS = os.environ.get('BMC_DEFAULT_PASS', '')
BMC_DOMAIN_SUFFIX = os.environ.get('BMC_DOMAIN_SUFFIX', '').strip()

# Optional chassis root SSH for LLDP / PCPU hops. Empty = feature off.
# Never bake key files or passwords into the tree; set only via host env.
ARESONE_ROOT_SSH_USER = os.environ.get('ARESONE_ROOT_SSH_USER', '').strip()
ARESONE_ROOT_SSH_PASSWORD = os.environ.get('ARESONE_ROOT_SSH_PASSWORD', '')
ARESONE_ROOT_SSH_KEY = os.environ.get('ARESONE_ROOT_SSH_KEY', '').strip()
KCOS_ROOT_SSH_USER = os.environ.get('KCOS_ROOT_SSH_USER', '').strip()
KCOS_ROOT_SSH_PASSWORD = os.environ.get('KCOS_ROOT_SSH_PASSWORD', '')
KCOS_ROOT_SSH_KEY = os.environ.get('KCOS_ROOT_SSH_KEY', '').strip()
KCOS_ROOT_SSH_PORT = int(os.environ.get('KCOS_ROOT_SSH_PORT', '9022') or '9022')

# ============================================================
# Keysight reservation notifications
# ============================================================
_recipients = os.environ.get('KEYSIGHT_EMAIL_RECIPIENTS', '')
KEYSIGHT_RESERVATION_EMAIL_RECIPIENTS = (
    [r.strip() for r in _recipients.split(',') if r.strip()]
    if _recipients
    else []
)

# ============================================================
# LDAP / Active Directory  (optional — set LDAP_SERVER_URI to enable)
# When the env-var is empty or missing the block is skipped entirely
# and LabVault continues to use local Django authentication.
# ============================================================
_ldap_uri = os.environ.get('LDAP_SERVER_URI', '')

if _ldap_uri:
    import ldap
    from django_auth_ldap.config import LDAPSearch, GroupOfNamesType

    AUTH_LDAP_SERVER_URI = _ldap_uri
    AUTH_LDAP_BIND_DN = os.environ.get('LDAP_BIND_DN', '')
    AUTH_LDAP_BIND_PASSWORD = os.environ.get('LDAP_BIND_PASSWORD', '')

    AUTH_LDAP_USER_SEARCH = LDAPSearch(
        os.environ.get('LDAP_USER_SEARCH_BASE', 'DC=keysight,DC=com'),
        ldap.SCOPE_SUBTREE,
        os.environ.get('LDAP_USER_SEARCH_FILTER', '(sAMAccountName=%(user)s)'),
    )

    AUTH_LDAP_USER_ATTR_MAP = {
        'first_name': 'givenName',
        'last_name': 'sn',
        'email': 'mail',
    }

    AUTH_LDAP_GROUP_SEARCH = LDAPSearch(
        os.environ.get('LDAP_GROUP_SEARCH_BASE', 'DC=keysight,DC=com'),
        ldap.SCOPE_SUBTREE,
        '(objectClass=group)',
    )
    AUTH_LDAP_GROUP_TYPE = GroupOfNamesType(name_attr='cn')

    _staff_group = os.environ.get('LDAP_STAFF_GROUP_DN', '')
    _superuser_group = os.environ.get('LDAP_SUPERUSER_GROUP_DN', '')
    AUTH_LDAP_USER_FLAGS_BY_GROUP = {}
    if _staff_group:
        AUTH_LDAP_USER_FLAGS_BY_GROUP['is_staff'] = _staff_group
    if _superuser_group:
        AUTH_LDAP_USER_FLAGS_BY_GROUP['is_superuser'] = _superuser_group

    AUTH_LDAP_MIRROR_GROUPS = os.environ.get(
        'LDAP_MIRROR_GROUPS', 'False',
    ).lower() in ('1', 'true', 'yes')

    AUTH_LDAP_ALWAYS_UPDATE_USER = True

    if os.environ.get('LDAP_START_TLS', 'False').lower() in ('1', 'true', 'yes'):
        AUTH_LDAP_START_TLS = True
    if os.environ.get('LDAP_IGNORE_CERT', 'False').lower() in ('1', 'true', 'yes'):
        AUTH_LDAP_GLOBAL_OPTIONS = {
            ldap.OPT_X_TLS_REQUIRE_CERT: ldap.OPT_X_TLS_NEVER,
        }

    AUTHENTICATION_BACKENDS = [
        'django_auth_ldap.backend.LDAPBackend',
        'django.contrib.auth.backends.ModelBackend',
    ]

# Unified change log (status, moves, deploy actions)
CHANGELOG_ENABLED = True
CHANGELOG_TEMP_DOWN_DAYS = float(os.environ.get('CHANGELOG_TEMP_DOWN_DAYS', '1'))
CHANGELOG_EXTENDED_DOWN_DAYS = float(os.environ.get('CHANGELOG_EXTENDED_DOWN_DAYS', '3'))

# Unified change log (status, moves, deploy actions)
CHANGELOG_ENABLED = True
CHANGELOG_TEMP_DOWN_DAYS = float(os.environ.get('CHANGELOG_TEMP_DOWN_DAYS', '1'))
CHANGELOG_EXTENDED_DOWN_DAYS = float(os.environ.get('CHANGELOG_EXTENDED_DOWN_DAYS', '3'))


# --- Customer public SKU baked defaults ---
LABVAULT_CUSTOMER_SKU = True
LABVAULT_EXTERNAL_PLUGINS_ENABLED = False
LABVAULT_TOPOLOGY_WIZARD_V1_ENABLED = False
LABVAULT_DEEP_DIAGNOSTICS_ENABLED = False
LABVAULT_DEMO_MODE = False
LABVAULT_AUTO_SEED = False
LABVAULT_WORKER_DEFAULT_MODE = "idle"
LABVAULT_CLI_ENABLED = True
WORKER_MODE_ENV = os.environ.get("LABVAULT_WORKER_MODE", "").strip().lower()
