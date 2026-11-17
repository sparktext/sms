"""
sparktext/celery.py
--------------------
Celery application entry point for SparkText.
"""
import os

from celery import Celery

# Set the default Django settings module for celery workers
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sparktext.settings")

app = Celery("sparktext")

# Load config from Django settings using the CELERY_ namespace
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks in all INSTALLED_APPS
app.autodiscover_tasks()

