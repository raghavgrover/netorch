"""
api/auth.py — Bearer token authentication dependency for FastAPI.

Every route that requires auth should declare:
    Depends(require_auth)

The token is read from netorch.toml [server] auth_token.
"""
from fastapi import Header, HTTPException, status
from core.config import server


async def require_auth(authorization: str = Header(...)) -> None:
    """
    Validates the Authorization: Bearer <token> header.
    Raises 401 if missing or wrong.
    """
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token != server.auth_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
