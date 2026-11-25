"""Shared release and Compose web server settings."""

bind = "0.0.0.0:8000"
worker_class = "asgi"
asgi_lifespan = "on"
workers = 1
timeout = 180
accesslog = "-"
logger_class = "config.privacy.PrivateAccessLogger"


def post_worker_init(worker):
    worker.log.start_request_activity()


def worker_exit(server, worker):
    worker.log.stop_request_activity()
