from .email import (
    EmailError,
    build_jobs_html,
    parse_recipients,
    send_email,
    smtp_configured,
)

__all__ = [
    "EmailError",
    "build_jobs_html",
    "parse_recipients",
    "send_email",
    "smtp_configured",
]
