"""
base.py — Main Django settings for Horilla
"""

import os
import sys
from datetime import timedelta
from os.path import join
from pathlib import Path

import environ
from django.contrib.messages import constants as messages
from django.core.files.storage import FileSystemStorage

# ========================================
# BASE PATH & ENVIRONMENT CONFIGURATION
# ========================================
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, True),
    SECRET_KEY=(str, "django-insecure-default-key"),
    ALLOWED_HOSTS=(list, ["*"]),
    CSRF_TRUSTED_ORIGINS=(list, ["http://localhost:8000"]),
    SECURE_SSL_REDIRECT=(bool, False),
)

# Existing process environment (Compose, systemd, CI) wins over .env values.
env.read_env(os.path.join(BASE_DIR, ".env"), overwrite=False)

# ========================================
# CORE DJANGO SETTINGS
# ========================================
SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")
HORILLA_ENV = env("HORILLA_ENV", default="")
REDIS_URL = env("REDIS_URL", default=None)

# Default site ID for django.contrib.sites framework.
SITE_ID = 1

THEME_APP = "horilla_theme"

INSTALLED_APPS = [
    # Default Django apps
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    # Third-party apps
    "notifications",
    "mathfilters",
    "corsheaders",
    "simple_history",
    "django_filters",
    "widget_tweaks",
    "auditlog",
    "django_apscheduler",
    "rest_framework",
    "rest_framework_simplejwt",
    "drf_yasg",
    # Core Horilla apps
    "horilla_auth",
    THEME_APP,
    "base",
    "employee",
    "recruitment",
    "leave",
    "pms",
    "onboarding",
    "krew_company_onboarding",
    "asset",
    "attendance",
    "payroll",
    "accessibility",
    "horilla_audit",
    "horilla_widgets",
    "horilla_crumbs",
    "horilla_documents",
    "horilla_views",
    "horilla_automations",
    "horilla_api",
    "biometric",
    "helpdesk",
    "offboarding",
    "horilla_backup",
    "project",
    "horilla_meet",
    "report",
    "whatsapp",
    "horilla_ldap",
    "horilla_dbtemplate",
    "horilla_tour",
]

# ========================================
# REST FRAMEWORK CONFIGURATION
# ========================================

REST_FRAMEWORK = {
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "PAGE_SIZE": 20,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
}

SWAGGER_SETTINGS = {
    "SECURITY_DEFINITIONS": {
        "Bearer": {
            "type": "apiKey",
            "name": "Authorization",
            "in": "header",
            "description": "Enter your Bearer token here",
        },
        "Basic": {"type": "basic", "description": "Basic authentication."},
    },
    "SECURITY": [{"Bearer": []}, {"Basic": []}],
}

APSCHEDULER_DATETIME_FORMAT = "N j, Y, f:s a"

APSCHEDULER_RUN_NOW_TIMEOUT = 25  # Seconds

# ========================================
# MIDDLEWARE
# ========================================
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Horilla-specific middlewares
    "base.middleware.CompanyMiddleware",
    "base.middleware.ForcePasswordChangeMiddleware",
    "base.middleware.TwoFactorAuthMiddleware",
    "accessibility.middlewares.AccessibilityMiddleware",
    "horilla.horilla_middlewares.MethodNotAllowedMiddleware",
    "horilla.horilla_middlewares.SVGSecurityMiddleware",
    "horilla.horilla_middlewares.MissingParameterMiddleware",
    "auditlog.middleware.AuditlogMiddleware",
]

ROOT_URLCONF = "horilla.urls"

# ========================================
# DATABASE CONFIGURATION
# ========================================
if env("DATABASE_URL", default=None):
    DATABASES = {"default": env.db()}
else:
    DATABASES = {
        "default": {
            "ENGINE": env("DB_ENGINE", default="django.db.backends.sqlite3"),
            "NAME": env("DB_NAME", default=os.path.join(BASE_DIR, "TestDB.sqlite3")),
            "USER": env("DB_USER", default=""),
            "PASSWORD": env("DB_PASSWORD", default=""),
            "HOST": env("DB_HOST", default=""),
            "PORT": env("DB_PORT", default=""),
            "OPTIONS": {
                "timeout": 30,  # seconds to wait on a locked DB before raising OperationalError
            },
        }
    }

# SQLite: enable WAL so reads (list/search) don't block session writes from
# concurrent requests like notification polling.
from django.db.backends.signals import connection_created


def _configure_sqlite_connection(sender, connection, **kwargs):
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")


connection_created.connect(_configure_sqlite_connection)

# ========================================
# CACHE (optional Redis when REDIS_URL is set)
# ========================================
# Fresh clones / runserver keep Django's default LocMem cache.
# Docker Compose sets REDIS_URL so the Redis service is actually used
# (requires django-redis in requirements.txt).
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                # Redis is a cache and nothing else, so losing it must degrade
                # to "slower", never to a 500. Without this a Redis outage
                # turns every cached page into an error and makes a disposable
                # dependency a hard one.
                "IGNORE_EXCEPTIONS": True,
            },
            "KEY_PREFIX": "krew",
        }
    }
    # Report swallowed cache errors instead of failing silently.
    DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS = True

# ========================================
# STATIC & MEDIA FILES
# ========================================
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
# STATICFILES_STORAGE (legacy, pre-Django-4.2) is no longer read by Django
# at all as of 5.x -- STORAGES (below) is what actually takes effect. Kept
# in sync in case any third-party code still reads the legacy attribute.
STATICFILES_STORAGE = "whitenoise.storage.CompressedStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media/")

# Same story for file storage: DEFAULT_FILE_STORAGE (legacy) is a no-op
# under Django 5.x. addons.py mutates STORAGES["default"]["BACKEND"]
# in-place to switch to S3 when AWS credentials are configured.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": STATICFILES_STORAGE,
    },
}

# ========================================
# AUTHENTICATION & SECURITY
# ========================================
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTH_USER_MODEL = "horilla_auth.HorillaUser"

X_FRAME_OPTIONS = "SAMEORIGIN"

# ========================================
# TEMPLATES
# ========================================
# In production (DEBUG=False) these are wrapped in the cached template
# loader so Django compiles each template once per process instead of
# re-parsing it (and re-running horilla_dbtemplate's DB-lookup chain) on
# every include, on every request. Left uncached in DEBUG so template
# edits during development are picked up without restarting the server.
_TEMPLATE_LOADERS = [
    "horilla_dbtemplate.loaders.Loader",
    ("django.template.loaders.filesystem.Loader", [BASE_DIR / THEME_APP / "templates"]),
    "django.template.loaders.app_directories.Loader",
    ("django.template.loaders.filesystem.Loader", [BASE_DIR / "templates"]),
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": False,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Horilla dynamic context processors
                "horilla.config.get_MENUS",
                "base.context_processors.get_companies",
                "base.context_processors.white_labelling_company",
                "base.context_processors.doc_base_url",
                "base.context_processors.resignation_request_enabled",
                "base.context_processors.timerunner_enabled",
                "base.context_processors.intial_notice_period",
                "base.context_processors.check_candidate_self_tracking",
                "base.context_processors.check_candidate_self_tracking_rating",
                "base.context_processors.get_initial_prefix",
                "base.context_processors.biometric_app_exists",
                "base.context_processors.enable_late_come_early_out_tracking",
                "base.context_processors.enable_profile_edit",
                "base.context_processors.export_access_enabled",
                "base.context_processors.navbar_languages",
                "horilla_tour.context_processors.pending_tours_flag",
                "horilla_crumbs.context_processors.breadcrumbs",
            ],
            "loaders": (
                _TEMPLATE_LOADERS
                if DEBUG
                else [("django.template.loaders.cached.Loader", _TEMPLATE_LOADERS)]
            ),
        },
    },
]

WSGI_APPLICATION = "horilla.wsgi.application"

# ========================================
# INTERNATIONALIZATION
# ========================================
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="Asia/Kolkata")
USE_I18N = True
USE_TZ = True

LANGUAGES = (
    ("en", "English (US)"),
    ("de", "Deutsch"),
    ("es", "Español"),
    ("fr", "Français"),
    ("ar", "العربية"),
    ("pt-br", "Português (Brasil)"),
    ("zh-hans", "简体中文"),
    ("zh-hant", "繁體中文"),
    ("it", "Italian"),
    ("tr", "Turkish"),
    ("uk", "Українська"),
)

LOCALE_PATHS = [join(BASE_DIR, "horilla", "locale")]

# ========================================
# CELERY (background work off the clock-in/clock-out hot path --
# see attendance/tasks.py, horilla/celery.py)
# ========================================
# Same "Redis is optional" shape as CACHE above: reuses REDIS_URL as the
# broker, no separate infrastructure. Without it (bare venv/runserver,
# and CI -- unit-tests.yml runs against SQLite defaults with no
# REDIS_URL) tasks run synchronously in-process via
# CELERY_TASK_ALWAYS_EAGER, so neither a worker nor a broker is ever
# required to run the app or its test suite.
CELERY_BROKER_URL = REDIS_URL or "memory://"
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_ALWAYS_EAGER = env.bool(
    "CELERY_TASK_ALWAYS_EAGER",
    # `manage.py test` must never depend on a live external worker
    # actually consuming a task within the test's own lifetime -- forced
    # eager here regardless of REDIS_URL, same reasoning as CI
    # (unit-tests.yml) staying correct simply by never setting REDIS_URL
    # at all. A local dev environment with REDIS_URL configured (for
    # real async testing outside of `test`) hit exactly this gap before.
    default=not bool(REDIS_URL) or "test" in sys.argv,
)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
# How often sweep_stuck_background_tasks (attendance/tasks.py) re-enqueues
# any BackgroundAttendanceTask still PENDING/FAILED -- the retry path for
# a task whose .delay() never reached a worker, or whose handler raised.
CELERY_BEAT_SWEEP_INTERVAL_SECONDS = env.int(
    "CELERY_BEAT_SWEEP_INTERVAL_SECONDS", default=300
)
CELERY_BEAT_SCHEDULE = {
    "sweep-stuck-attendance-background-tasks": {
        "task": "attendance.tasks.sweep_stuck_background_tasks",
        "schedule": CELERY_BEAT_SWEEP_INTERVAL_SECONDS,
    },
}

# Shared TTL (seconds) for the hot-path caches in base/config_tiers.py
# (tiered AttendanceRuleSet/GeoFencing resolution) and attendance/caching.py
# (AttendanceGeneralSetting, shift schedule) -- a safety net only, since
# both are actually kept fresh by signal-based invalidation; this just
# bounds how long a missed signal (e.g. a bulk .update() that bypasses
# save()) could serve a stale value.
CACHE_TTL_SECONDS = env.int("CACHE_TTL_SECONDS", default=300)

# Automatic-retry cap for BackgroundAttendanceTask (attendance/models.py,
# attendance/tasks.py) -- both the periodic sweep and a manual retry read
# this as the attempt ceiling.
MAX_RETRIES = env.int("MAX_RETRIES", default=5)

# How long process_background_attendance_task's per-task lock (a Redis
# SETNX via cache.add(), attendance/tasks.py) is held before it's
# considered abandoned. This is a safety net for a worker that died mid-
# task, not the normal release path -- the task releases its own lock in
# a finally block the moment it finishes. Should comfortably exceed the
# slowest realistic run of late_come()/early_out()/flexible_shortfall().
BACKGROUND_TASK_LOCK_TTL_SECONDS = env.int(
    "BACKGROUND_TASK_LOCK_TTL_SECONDS", default=60
)

# ========================================
# LOGGING, MESSAGES, OTHER GLOBALS
# ========================================

# Without this, `logging.getLogger(__name__)` calls throughout the
# project (e.g. attendance/tasks.py) have no handler anywhere in their
# hierarchy -- Python's own "handler of last resort" then applies, which
# only prints WARNING and above, so every logger.info()/logger.debug()
# call in the codebase silently goes nowhere. LOG_LEVEL controls the
# project's OWN loggers (root, minus the exceptions below); bump it to
# DEBUG in .env for the more granular per-request tracing some call
# sites use (e.g. attendance/tasks.py's per-branch traces).
LOG_LEVEL = env("LOG_LEVEL", default="DEBUG" if DEBUG else "INFO")

LOGGING = {
    "version": 1,
    # Preserves Django's own already-working loggers (notably
    # django.server, which prints runserver's request access log lines
    # via its own separate handler/formatter) -- only loggers actually
    # redefined below are affected.
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        # Kept quiet even at LOG_LEVEL=DEBUG -- SQL query logging in
        # particular (one line per query) would otherwise drown out
        # everything else the project's own code logs.
        "django.db.backends": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "django.utils.autoreload": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        # django_apscheduler's own polling loop (attendance/scheduler.py's
        # Auto Punch-out job, and payroll's) logs "looking for jobs to
        # run"/"next wakeup" on every poll -- INFO+ only, even at
        # LOG_LEVEL=DEBUG.
        "apscheduler": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        # Celery/Kombu's own internals (task registry dumps, broker
        # connection chatter) -- dumps a burst of DEBUG noise the first
        # time a task runs in a given process (attendance/tasks.py under
        # CELERY_TASK_ALWAYS_EAGER). Not the project's own task logging,
        # which uses attendance.tasks's own logger, unaffected by this.
        "celery": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "kombu": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MESSAGE_TAGS = {
    messages.DEBUG: "oh-alert--warning",
    messages.INFO: "oh-alert--info",
    messages.SUCCESS: "oh-alert--success",
    messages.WARNING: "oh-alert--warning",
    messages.ERROR: "oh-alert--danger",
}

LOGIN_URL = "/login"
SIMPLE_HISTORY_REVERT_DISABLED = True

DJANGO_NOTIFICATIONS_CONFIG = {
    "USE_JSONFIELD": True,
    "SOFT_DELETE": True,
    "USE_WATCHED": True,
    "NOTIFICATIONS_STORAGE": "notifications.storage.DatabaseStorage",
    "TEMPLATE": "notifications.html",
}

# ========================================
# HORILLA-SPECIFIC SETTINGS
# ========================================
WHITE_LABELLING = False
NESTED_SUBORDINATE_VISIBILITY = False
TWO_FACTORS_AUTHENTICATION = False

SIDEBARS = [
    "krew_company_onboarding",
    "employee",
    "attendance",
    "leave",
    "payroll",
    "recruitment",
    "onboarding",
    "offboarding",
    "pms",
    "project",
    "asset",
    "helpdesk",
    "report",
]

# Audit logging is opt-in: the horilla_audit app registers models explicitly
# through its registry, driven by AuditModelConfig and a default whitelist
# (Employee, EmployeeWorkInformation, EmployeeBankDetails).
AUDITLOG_INCLUDE_ALL_MODELS = False
AUDITLOG_EXCLUDE_TRACKING_MODELS = (
    # "<app_name>",
    # "<app_name>.<model>"
)

EMAIL_BACKEND = "base.backends.ConfiguredEmailBackend"

"""
DB_INIT_PASSWORD: str

The password used for database setup and initialization. This password is a
48-character alphanumeric string generated using a UUID to ensure high entropy and security.
"""
DB_INIT_PASSWORD = env(
    "DB_INIT_PASSWORD", default="d3f6a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d"
)

# ========================================
# PERMISSIONS / CUSTOM LOGIC
# ========================================
# When True, group permissions are scoped per company via
# base.models.CompanyGroupAssignment (resolved by CompanyScopedBackend).
# When False, legacy behavior: user.groups grant permissions globally.
# Instant rollback switch: set the COMPANY_SCOPED_PERMISSIONS env var to False.
COMPANY_SCOPED_PERMISSIONS = env.bool("COMPANY_SCOPED_PERMISSIONS", default=True)

NO_PERMISSION_MODALS = [
    "companygroupassignment",
    "historicalbonuspoint",
    "assetreport",
    "assetdocuments",
    "returnimages",
    "holiday",
    "companyleave",
    "historicalavailableleave",
    "historicalleaverequest",
    "historicalleaveallocationrequest",
    "leaverequestconditionapproval",
    "historicalcompensatoryleaverequest",
    "employeepastleaverestrict",
    "overrideleaverequests",
    "historicalrotatingworktypeassign",
    "employeeshiftday",
    "historicalrotatingshiftassign",
    "historicalworktyperequest",
    "historicalshiftrequest",
    "multipleapprovalmanagers",
    "attachment",
    "announcementview",
    "emaillog",
    "driverviewed",
    "dashboardemployeecharts",
    "attendanceallowedip",
    "tracklatecomeearlyout",
    "historicalcontract",
    "overrideattendance",
    "overrideleaverequest",
    "overrideworkinfo",
    "multiplecondition",
    "historicalpayslip",
    "reimbursementmultipleattachment",
    "workrecord",
    "historicalticket",
    "skill",
    "historicalcandidate",
    "rejectreason",
    "historicalrejectedcandidate",
    "rejectedcandidate",
    "stagefiles",
    "stagenote",
    "questionordering",
    "recruitmentsurveyordering",
    "recruitmentsurveyanswer",
    "recruitmentgeneralsetting",
    "resume",
    "recruitmentmailtemplate",
    "profileeditfeature",
]

FILE_STORAGE = FileSystemStorage(location="csv_tmp/")

HORILLA_DATE_FORMATS = {
    "DD/MM/YY": "%d/%m/%y",
    "DD-MM-YYYY": "%d-%m-%Y",
    "DD.MM.YYYY": "%d.%m.%Y",
    "DD/MM/YYYY": "%d/%m/%Y",
    "MM/DD/YYYY": "%m/%d/%Y",
    "YYYY-MM-DD": "%Y-%m-%d",
    "YYYY/MM/DD": "%Y/%m/%d",
    "MMMM D, YYYY": "%B %d, %Y",
    "DD MMMM, YYYY": "%d %B, %Y",
    "MMM. D, YYYY": "%b. %d, %Y",
    "D MMM. YYYY": "%d %b. %Y",
    "dddd, MMMM D, YYYY": "%A, %B %d, %Y",
}

HORILLA_TIME_FORMATS = {
    "hh:mm A": "%I:%M %p",  # 12-hour format
    "HH:mm": "%H:%M",  # 24-hour format
    "HH:mm:ss.SSSSSS": "%H:%M:%S.%f",  # 24-hour format with seconds and microseconds
}

BIO_DEVICE_THREADS = {}

DYNAMIC_URL_PATTERNS = []

APP_URLS = [
    "base.urls",
    "employee.urls",
]

APPS = [
    "auth",
    "base",
    "employee",
    "horilla_documents",
    "horilla_automations",
]

# ========================================
# LDAP CONFIGURATION (Default)
# ========================================
AUTH_LDAP_SERVER_URI = env("AUTH_LDAP_SERVER_URI", default="ldap://127.0.0.1:389")
AUTH_LDAP_BIND_DN = env("AUTH_LDAP_BIND_DN", default="cn=admin,dc=horilla,dc=com")
AUTH_LDAP_BIND_PASSWORD = env("AUTH_LDAP_BIND_PASSWORD", default="")

AUTH_LDAP_USER_ATTR_MAP = {
    "first_name": "givenName",
    "last_name": "sn",
    "email": "mail",
}

# Default LDAP settings
DEFAULT_LDAP_CONFIG = {
    "LDAP_SERVER": env("LDAP_SERVER", default="ldap://127.0.0.1:389"),
    "BIND_DN": env("BIND_DN", default="cn=admin,dc=horilla,dc=com"),
    "BIND_PASSWORD": env("BIND_PASSWORD", default=""),
    "BASE_DN": env("BASE_DN", default="ou=users,dc=horilla,dc=com"),
}

# CompanyScopedBackend subclasses ModelBackend; it behaves identically while
# COMPANY_SCOPED_PERMISSIONS is False. It must REPLACE ModelBackend (Django
# unions grants across backends, so listing both would keep global perms).
AUTHENTICATION_BACKENDS = [
    "base.auth_backends.CompanyScopedBackend",
    # "django_auth_ldap.backend.LDAPBackend",
]

AUTH_LDAP_ALWAYS_UPDATE_USER = True

# ========================================
# PRODUCTION SECURITY GATES
# ========================================
# Fail closed when DEBUG=False or HORILLA_ENV=production. Local DEBUG=True
# tutorials keep insecure-but-documented defaults for open-source onboarding.
from horilla.settings.security import (  # noqa: E402
    apply_secure_defaults,
    is_production_mode,
    validate_production_secrets,
)

IS_PRODUCTION = is_production_mode(DEBUG, HORILLA_ENV)

if IS_PRODUCTION:
    validate_production_secrets(SECRET_KEY, ALLOWED_HOSTS, DB_INIT_PASSWORD)

if not DEBUG:
    globals().update(apply_secure_defaults(env, DEBUG))
