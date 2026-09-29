"""
horilla/celery.py

The Celery app for background work off the clock-in/clock-out hot path
(Part 3 of the Attendance performance plan -- see attendance/tasks.py).
Standard Django+Celery wiring: config comes from Django settings under
the CELERY_ namespace (horilla/settings/base.py), tasks are
autodiscovered from each installed app's tasks.py.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "horilla.settings")

app = Celery("horilla")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
