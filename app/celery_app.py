import sentry_sdk
from celery import Celery

from app.config import settings

# Initialize Sentry for the Celery worker process as well
if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=0.2,
        send_default_pii=False,
    )

celery = Celery(
    "rufus_generator",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_max_tasks_per_child=settings.celery_worker_max_tasks,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "app.tasks.process_asin.enqueue_job": {"queue": "interactive"},
        "app.tasks.process_asin.process_single_asin": {"queue": "bulk"},
    },
    task_default_queue="bulk",
    task_queues={
        "bulk": {"exchange": "bulk", "routing_key": "bulk"},
        "interactive": {"exchange": "interactive", "routing_key": "interactive"},
    },
)

celery.autodiscover_tasks(["app.tasks"])
