"""
webapp/api/auth.py

Email/password authentication + anonymous session support.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webapp import config
from webapp.db import get_db
from webapp.db.models import User

router = APIRouter()

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

ALGORITHM = "HS256"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _create_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=config.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    return jwt.encode(
        {"sub": user_id, "exp": expire},
        config.SECRET_KEY,
        algorithm=ALGORITHM,
    )


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI dependency: returns the authenticated user or raises 401."""
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        payload = jwt.decode(token, config.SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


async def get_optional_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Dependency: returns authenticated user if token present, otherwise creates
    an anonymous user and returns a token in the response header (handled by
    calling code).  For simplicity, anonymous sessions always get a token too.
    """
    if token:
        try:
            return await get_current_user(token, db)
        except HTTPException:
            pass

    # Create anonymous user
    user = User(id=str(uuid.uuid4()), is_anonymous=True)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/register", response_model=TokenResponse)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=body.email,
        hashed_password=pwd_context.hash(body.password),
        is_anonymous=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return TokenResponse(access_token=_create_token(user.id), user_id=user.id)


@router.post("/login", response_model=TokenResponse)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == form.username))
    user = result.scalar_one_or_none()
    if user is None or not pwd_context.verify(form.password, user.hashed_password or ""):
        raise HTTPException(status_code=400, detail="Incorrect email or password")

    user.last_seen = datetime.now(timezone.utc)
    await db.commit()
    return TokenResponse(access_token=_create_token(user.id), user_id=user.id)


@router.post("/anonymous", response_model=TokenResponse)
async def anonymous_session(db: AsyncSession = Depends(get_db)):
    """Create an anonymous user and return a short-lived token."""
    user = User(is_anonymous=True)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return TokenResponse(access_token=_create_token(user.id), user_id=user.id)
