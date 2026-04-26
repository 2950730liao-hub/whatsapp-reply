"""
SaaS 许可证验证与 JWT 认证
"""
import os
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException, Depends, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

try:
    import jwt
except ImportError:
    import jose.jwt as jwt

SECRET_KEY = os.getenv("SAAS_JWT_SECRET", "saas-whatsapp-secret-change-in-production")
ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 7

security = HTTPBearer()


def create_device_token(tenant_id: int, device_id: int, machine_id: str) -> str:
    """签发设备 JWT"""
    payload = {
        "tenant_id": tenant_id,
        "device_id": device_id,
        "machine_id": machine_id,
        "exp": datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS),
        "iat": datetime.utcnow(),
        "type": "device",
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_device_token(token: str) -> dict:
    """解码并验证 device JWT"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "device":
            raise HTTPException(status_code=401, detail="无效的 token 类型")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="device_token 已过期，请重新认证")
    except Exception:
        raise HTTPException(status_code=401, detail="无效的 device_token")


def get_current_device(
    credentials: HTTPAuthorizationCredentials = Security(security),
    db: Session = None,
):
    """FastAPI 依赖：从 Bearer token 获取当前设备信息"""
    from saas.models import ClientDevice, Tenant
    payload = decode_device_token(credentials.credentials)
    device = db.query(ClientDevice).filter(ClientDevice.id == payload["device_id"]).first()
    if not device:
        raise HTTPException(status_code=401, detail="设备不存在")
    if device.status == "banned":
        raise HTTPException(status_code=403, detail="设备已被禁用")
    tenant = db.query(Tenant).filter(Tenant.id == device.tenant_id).first()
    if not tenant or tenant.status != "active":
        raise HTTPException(status_code=403, detail="租户账户无效或已暂停")
    return device, tenant


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed
