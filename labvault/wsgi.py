"""WSGI entry point (``labvault.wsgi:application``) served by gunicorn on loopback :8000.

Uses ``connect.settings`` unless ``DJANGO_SETTINGS_MODULE`` is already set.
"""
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'connect.settings')  # Ensure this points to your settings module

application = get_wsgi_application()
