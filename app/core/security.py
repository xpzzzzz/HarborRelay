import base64
import hashlib
import hmac
import re
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.entities import UserAccount
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


_password_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    if re.fullmatch(r"[0-9a-f]{64}", hashed):
        return hmac.compare_digest(hashlib.sha256(password.encode("utf-8")).hexdigest(), hashed)
    try:
        return _password_hasher.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


def _credentials(request: Request) -> tuple[str, str]:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Basic authorization")
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Basic authorization") from exc


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> UserAccount:
    username, password = _credentials(request)
    user = db.query(UserAccount).filter(UserAccount.username == username).first()
    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad credentials")
    if not user.password_hash.startswith("$argon2id$") or _password_hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()
    return user


def require_admin(user: Annotated[UserAccount, Depends(current_user)]) -> UserAccount:
    if "ROLE_ADMIN" not in user.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user


def require_staff(user: Annotated[UserAccount, Depends(current_user)]) -> UserAccount:
    if not {"ROLE_ADMIN", "ROLE_COUNSELOR"}.intersection(user.roles):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff role required")
    return user


def authorize_case(db: Session, case, actor: UserAccount) -> UserAccount:
    if not isinstance(actor, UserAccount) or actor.id is None:
        raise PermissionError("Authenticated principal required")
    principal = db.get(UserAccount, actor.id)
    if principal is None:
        raise PermissionError("Unknown principal")
    if "ROLE_ADMIN" in principal.roles:
        return principal
    if "ROLE_COUNSELOR" in principal.roles and case.owner in {"unassigned", principal.username}:
        return principal
    raise PermissionError("Case access denied")

