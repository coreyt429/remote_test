# /home/ubuntu/api/celery_app.py
import os
from celery import Celery

def make_celery() -> Celery:
    redis_url = os.getenv("REDIS_URL", "redis://systemd-redis:6379/0")
    app = Celery(
        "api",
        broker=redis_url,
        backend=os.getenv("CELERY_RESULT_BACKEND", redis_url),
        include=["tasks"],
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        task_acks_late=True,
        worker_prefetch_multiplier=1,
    )
    return app

celery_app = make_celery()

