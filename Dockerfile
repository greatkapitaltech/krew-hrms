# Build stage - for compiling dependencies
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install build dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libjpeg-dev \
        zlib1g-dev \
        libcairo2-dev \
        libpango1.0-dev \
        libgdk-pixbuf-xlib-2.0-dev \
        libxml2-dev \
        libxslt1-dev \
        libffi-dev \
        pkg-config \
        gcc \
        g++ \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt gunicorn psycopg2-binary

# Production stage - minimal runtime image
FROM python:3.12-slim AS production

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Install only runtime dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        libjpeg62-turbo \
        zlib1g \
        libcairo2 \
        libpango-1.0-0 \
        libgdk-pixbuf-xlib-2.0-0 \
        libxml2 \
        libxslt1.1 \
        libffi8 \
        curl \
        ca-certificates \
        netcat-openbsd \
        fontconfig \
        fonts-dejavu-core \
        xfonts-75dpi \
        xfonts-base \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# wkhtmltopdf (payroll payslip PDFs, document_templates generated documents)
# is not in Debian's own repo as a reliably headless-capable build, so pull
# the official packaging project's static (patched-Qt) release instead -
# that's the same binary pdfkit's own docs point people to. Picks the .deb
# matching this image's architecture so the same Dockerfile builds on both
# amd64 and arm64 hosts.
RUN set -eux; \
    ARCH="$(dpkg --print-architecture)"; \
    curl -fsSL -o /tmp/wkhtmltox.deb \
        "https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.bookworm_${ARCH}.deb"; \
    apt-get update; \
    (dpkg -i /tmp/wkhtmltox.deb || apt-get install -y -f --no-install-recommends); \
    rm -f /tmp/wkhtmltox.deb; \
    rm -rf /var/lib/apt/lists/*; \
    apt-get clean; \
    wkhtmltopdf --version

# Create non-root user FIRST
RUN useradd --create-home --uid 1000 appuser

# Copy virtual environment from builder stage WITH correct ownership
COPY --from=builder --chown=appuser:appuser /opt/venv /opt/venv

WORKDIR /app

# Copy application code
COPY --chown=appuser:appuser . .

# Copy entrypoint script
COPY --chown=appuser:appuser docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create necessary directories and set permissions
RUN mkdir -p staticfiles media \
    && chown -R appuser:appuser /app

# Collect static files at BUILD time, not on every container start.
# 5,300+ files took ~60-90s off every single deploy when done at boot.
#
# No database is reachable during a build, so DATABASE_URL is deliberately left
# unset here: horilla/settings/base.py then falls back to SQLite, which is
# enough for collectstatic to import the app registry. The throwaway database
# is removed in the same layer so it is never shipped.
RUN SECRET_KEY=build-time-only-not-a-secret \
    DB_NAME=/tmp/build-collectstatic.sqlite3 \
    python manage.py collectstatic --noinput \
 && rm -f /tmp/build-collectstatic.sqlite3

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=30s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health/ || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "horilla.wsgi:application", "--config", "docker/gunicorn.conf.py"]
