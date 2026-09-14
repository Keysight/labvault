FROM python:3.11-slim
# libldap/sasl: python-ldap (django-auth-ldap); libpq: psycopg build fallback
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential libpq-dev libldap2-dev libsasl2-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .
COPY deploy/compose/entrypoint.sh /entrypoint.sh
RUN useradd -r -u 10001 labvault \
    && chmod +x /entrypoint.sh \
    && mkdir -p /var/lib/labvault/uhd-locks /var/lib/labvault/media /app/var/django_cache \
    && chown -R labvault:labvault /app /entrypoint.sh /var/lib/labvault
# Entrypoint starts as root so it can chown Compose named volumes, then drops to labvault.
ENV DJANGO_SETTINGS_MODULE=connect.settings
ENV HOME=/var/lib/labvault
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "labvault.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "300", "--graceful-timeout", "30", "--max-requests", "300", "--max-requests-jitter", "50"]
