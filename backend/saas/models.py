"""
SaaS 数据模型
租户、算力账户、设备、交易记录
"""
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, Float, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime
import secrets
import string

# 独立 Base，由 init_saas_db 与主数据库 engine 绑定
from database import Base


class Tenant(Base):
    """租户表"""
    __tablename__ = "saas_tenants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(200), unique=True, index=True)
    hashed_password = Column(Text)
    license_key = Column(String(64), unique=True, index=True, nullable=False)
    status = Column(String(20), default="active")       # active/suspended/expired
    max_whatsapp_accounts = Column(Integer, default=1)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    credit_account = relationship("CreditAccount", back_populates="tenant", uselist=False, cascade="all, delete-orphan")
    devices = relationship("ClientDevice", back_populates="tenant", cascade="all, delete-orphan")
    transactions = relationship("CreditTransaction", back_populates="tenant", cascade="all, delete-orphan")


class CreditAccount(Base):
    """算力账户（每个租户一个）"""
    __tablename__ = "saas_credit_accounts"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("saas_tenants.id"), unique=True, nullable=False, index=True)
    balance = Column(Float, default=0.0)
    total_recharged = Column(Float, default=0.0)
    total_consumed = Column(Float, default=0.0)
    alert_threshold = Column(Float, default=10.0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant = relationship("Tenant", back_populates="credit_account")


class CreditTransaction(Base):
    """算力交易记录"""
    __tablename__ = "saas_credit_transactions"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("saas_tenants.id"), nullable=False, index=True)
    type = Column(String(20), nullable=False)     # consume / recharge / refund
    amount = Column(Float, nullable=False)         # 正数=增加，负数=减少
    balance_after = Column(Float)
    description = Column(Text)
    tokens_used = Column(Integer, default=0)
    agent_id = Column(Integer)
    customer_phone = Column(String(30))
    recharge_order_id = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    tenant = relationship("Tenant", back_populates="transactions")


class ClientDevice(Base):
    """客户端设备"""
    __tablename__ = "saas_client_devices"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("saas_tenants.id"), nullable=False, index=True)
    device_name = Column(String(100))
    machine_id = Column(String(64), unique=True, index=True)
    device_token = Column(Text)
    whatsapp_phone = Column(String(30))
    platform = Column(String(20), default="windows")
    app_version = Column(String(20))
    status = Column(String(20), default="offline")   # online/offline/banned
    last_online = Column(DateTime)
    last_heartbeat = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant = relationship("Tenant", back_populates="devices")


def generate_license_key() -> str:
    """生成许可证密钥，格式：WXXX-XXXX-XXXX-XXXX"""
    chars = string.ascii_uppercase + string.digits
    segments = ['W' + ''.join(secrets.choice(chars) for _ in range(3))]
    segments += [''.join(secrets.choice(chars) for _ in range(4)) for _ in range(3)]
    return '-'.join(segments)
