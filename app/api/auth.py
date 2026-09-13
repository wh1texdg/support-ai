import secrets
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

bearer = HTTPBearer(auto_error=False)
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


def matches(value, expected):
    return bool(expected) and secrets.compare_digest(value, expected)


def admin(credentials: Credentials):
    if not credentials or not matches(credentials.credentials, settings().admin_api_key.get_secret_value()):
        raise HTTPException(401, "Admin authentication required")
    return "admin"


def bot_auth(credentials: Credentials):
    if not credentials or not matches(credentials.credentials, settings().bot_api_key.get_secret_value()):
        raise HTTPException(401, "Bot authentication required")


def operator(credentials: Credentials):
    if credentials:
        for key, identity in settings().operator_keys.items():
            if matches(credentials.credentials, key):
                return identity
    raise HTTPException(401, "Operator authentication required")


def staff(credentials: Credentials):
    if credentials and matches(credentials.credentials, settings().admin_api_key.get_secret_value()):
        return "admin"
    return operator(credentials)
