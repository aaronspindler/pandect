"""Shared release and Compose web server settings."""

bind = "0.0.0.0:8000"
worker_class = "asgi"
asgi_lifespan = "on"
workers = 1
timeout = 180
accesslog = "-"
logger_class = "config.privacy.PrivateAccessLogger"
