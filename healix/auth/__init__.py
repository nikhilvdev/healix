"""Automatic login: recognise a login page and authenticate with credentials from `.env`."""

from healix.auth.login_handler import (
    AUTH_REJECTED,
    AUTHENTICATED,
    BLOCKED,
    FAILED,
    MFA_REQUIRED,
    NOT_NEEDED,
    PASSWORD_ENV,
    SELECTOR_NOT_FOUND,
    SKIPPED,
    TIMEOUT,
    USERNAME_ENV,
    Authenticator,
    Credentials,
    LoginHandler,
    LoginResult,
    redact_url,
)

__all__ = [
    "AUTHENTICATED",
    "AUTH_REJECTED",
    "BLOCKED",
    "FAILED",
    "MFA_REQUIRED",
    "NOT_NEEDED",
    "PASSWORD_ENV",
    "SELECTOR_NOT_FOUND",
    "SKIPPED",
    "TIMEOUT",
    "USERNAME_ENV",
    "Authenticator",
    "Credentials",
    "LoginHandler",
    "LoginResult",
    "redact_url",
]
