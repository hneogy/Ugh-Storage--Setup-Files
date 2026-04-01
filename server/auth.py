"""JWT authentication for multi-tenant UghStorage.

Tokens are issued by the Supabase edge function (get-pi-token) and signed
with the device's shared secret.  This module verifies those tokens.
"""

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import (
    DEVICE_ID,
    DEVICE_SHARED_SECRET,
    JWT_ALGORITHM,
)

_bearer_scheme = HTTPBearer()


def verify_password(plain: str, hashed: str) -> bool:
    """Check a plaintext password against its bcrypt hash.

    Kept for backward compatibility during migration; not used in the
    main authentication flow.
    """
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


async def require_auth(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> str:
    """FastAPI dependency that enforces a valid JWT and returns the user UUID.

    The JWT is expected to contain:
      - sub: user UUID
      - device_id: device UUID (must match this Pi's DEVICE_ID)
      - iat: issued-at timestamp
      - exp: expiration timestamp

    The token is signed with DEVICE_SHARED_SECRET using HS256.
    """
    if not DEVICE_SHARED_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Device not registered. Complete BLE setup first.",
        )

    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            DEVICE_SHARED_SECRET,
            algorithms=[JWT_ALGORITHM],
        )

        user_id: str | None = payload.get("sub")
        if user_id is None:
            raise ValueError("missing sub")

        token_device_id: str | None = payload.get("device_id")
        if token_device_id is None:
            raise ValueError("missing device_id")

        if token_device_id != DEVICE_ID:
            raise ValueError("device_id mismatch")

        return user_id

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        )
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        )
