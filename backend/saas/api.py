"""
SaaS API 路由
客户端认证、心跳、算力计费、AI回复
"""
import os
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db, AIAgent, AgentKnowledgeBinding
from llm_service import get_llm_service
from knowledge_base import get_knowledge_base

from saas.models import Tenant, ClientDevice, CreditAccount, CreditTransaction
from saas.auth import create_device_token, get_current_device
from saas.credit import check_and_deduct_credits, recharge_credits, get_balance

router = APIRouter(prefix="/api/v1")

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "admin-change-me")


def require_admin(x_admin_key: str = Header(..., alias="X-Admin-Key")):
    if x_admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="无效的管理员密钥")
    return True


class LicenseAuthRequest(BaseModel):
    license_key: str
    machine_id: str
    device_name: Optional[str] = "Unknown Device"
    platform: Optional[str] = "windows"
    app_version: Optional[str] = "1.0.0"


class LicenseAuthResponse(BaseModel):
    device_token: str
    tenant_id: int
    tenant_name: str
    expires_at: str
    max_whatsapp_accounts: int


class HeartbeatRequest(BaseModel):
    whatsapp_connected: bool = False
    message_count_period: int = 0
    credit_consumed_period: float = 0.0
    whatsapp_phone: Optional[str] = None


class HeartbeatResponse(BaseModel):
    license_status: str
    alert_messages: List[str] = []


class ChatReplyRequest(BaseModel):
    customer_phone: str
    customer_name: Optional[str] = "客户"
    messages: List[dict] = []
    agent_id: Optional[int] = None


class ChatReplyResponse(BaseModel):
    reply: str
    credit_consumed: float
    balance_remaining: float


class RechargeRequest(BaseModel):
    tenant_id: int
    amount: float = Field(..., gt=0)
    description: Optional[str] = "手动充值"
    order_id: Optional[str] = None


@router.post("/auth/license", response_model=LicenseAuthResponse)
async def auth_license(req: LicenseAuthRequest, db: Session = Depends(get_db)):
    tenant = db.query(Tenant).filter(Tenant.license_key == req.license_key.strip()).first()
    if not tenant:
        raise HTTPException(status_code=401, detail="无效的许可证密钥")
    if tenant.status != "active":
        raise HTTPException(status_code=403, detail=f"租户状态: {tenant.status}")

    device = db.query(ClientDevice).filter(ClientDevice.machine_id == req.machine_id).first()
    if device:
        if device.tenant_id != tenant.id:
            raise HTTPException(status_code=403, detail="设备已绑定到其他租户")
        device.device_name = req.device_name
        device.platform = req.platform
        device.app_version = req.app_version
        device.status = "online"
        device.last_online = datetime.utcnow()
        device.last_heartbeat = datetime.utcnow()
    else:
        device_count = db.query(ClientDevice).filter(ClientDevice.tenant_id == tenant.id).count()
        if device_count >= tenant.max_whatsapp_accounts:
            raise HTTPException(status_code=403, detail=f"设备数量已达上限 ({tenant.max_whatsapp_accounts})")
        device = ClientDevice(
            tenant_id=tenant.id, device_name=req.device_name, machine_id=req.machine_id,
            platform=req.platform, app_version=req.app_version, status="online",
            last_online=datetime.utcnow(), last_heartbeat=datetime.utcnow(),
        )
        db.add(device)
        db.commit()
        db.refresh(device)

    device_token = create_device_token(tenant.id, device.id, req.machine_id)
    device.device_token = device_token
    db.commit()

    return LicenseAuthResponse(
        device_token=device_token, tenant_id=tenant.id, tenant_name=tenant.name,
        expires_at=(datetime.utcnow().replace(day=datetime.utcnow().day + 7)).isoformat(),
        max_whatsapp_accounts=tenant.max_whatsapp_accounts,
    )


@router.post("/device/heartbeat", response_model=HeartbeatResponse)
async def device_heartbeat(req: HeartbeatRequest, device_tenant=Depends(get_current_device), db: Session = Depends(get_db)):
    device, tenant = device_tenant
    device.status = "online"
    device.last_heartbeat = datetime.utcnow()
    device.last_online = datetime.utcnow()
    if req.whatsapp_phone:
        device.whatsapp_phone = req.whatsapp_phone
    db.commit()

    alert_messages = []
    account = db.query(CreditAccount).filter(CreditAccount.tenant_id == tenant.id).first()
    if account and account.balance <= account.alert_threshold:
        alert_messages.append(f"算力余额不足预警: 当前余额 {account.balance:.2f}")
    return HeartbeatResponse(license_status=tenant.status, alert_messages=alert_messages)


@router.get("/credit/balance")
async def credit_balance(device_tenant=Depends(get_current_device), db: Session = Depends(get_db)):
    device, tenant = device_tenant
    return get_balance(tenant.id, db)


@router.get("/credit/transactions")
async def credit_transactions(limit: int = 50, offset: int = 0, device_tenant=Depends(get_current_device), db: Session = Depends(get_db)):
    device, tenant = device_tenant
    txs = db.query(CreditTransaction).filter(CreditTransaction.tenant_id == tenant.id).order_by(CreditTransaction.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "transactions": [
            {"id": tx.id, "type": tx.type, "amount": tx.amount, "balance_after": tx.balance_after,
             "description": tx.description, "tokens_used": tx.tokens_used,
             "customer_phone": tx.customer_phone, "created_at": tx.created_at.isoformat() if tx.created_at else None}
            for tx in txs
        ],
        "total": db.query(CreditTransaction).filter(CreditTransaction.tenant_id == tenant.id).count(),
    }


@router.post("/credit/recharge")
async def credit_recharge(req: RechargeRequest, db: Session = Depends(get_db), admin: bool = Depends(require_admin)):
    tenant = db.query(Tenant).filter(Tenant.id == req.tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="租户不存在")
    result = recharge_credits(tenant_id=req.tenant_id, amount=req.amount, db=db, order_id=req.order_id, description=req.description)
    return {"success": True, "tenant_id": req.tenant_id, **result}


@router.post("/chat/reply", response_model=ChatReplyResponse)
async def chat_reply(req: ChatReplyRequest, device_tenant=Depends(get_current_device), db: Session = Depends(get_db)):
    device, tenant = device_tenant
    deduct_result = check_and_deduct_credits(
        tenant_id=tenant.id, db=db, description=f"AI回复: {req.customer_phone}", customer_phone=req.customer_phone,
    )

    agent = None
    if req.agent_id:
        agent = db.query(AIAgent).filter(AIAgent.id == req.agent_id).first()

    knowledge_base = None
    if agent:
        kb = get_knowledge_base()
        bindings = db.query(AgentKnowledgeBinding).filter(AgentKnowledgeBinding.agent_id == agent.id).all()
        if bindings:
            doc_ids = [b.knowledge_doc_id for b in bindings]
            knowledge_text = kb.get_documents_by_ids(doc_ids)
            if knowledge_text:
                knowledge_base = knowledge_text

    llm = get_llm_service()
    customer = {"name": req.customer_name, "phone": req.customer_phone}

    try:
        if agent:
            reply = await llm.generate_reply_with_agent(
                customer=customer, messages=req.messages, agent=agent,
                knowledge_base=knowledge_base, db=db,
            )
        else:
            reply = await llm.generate_reply(customer=customer, messages=req.messages, knowledge=knowledge_base or "", db=db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI生成失败: {str(e)}")

    return ChatReplyResponse(reply=reply, credit_consumed=deduct_result["credit_consumed"], balance_remaining=deduct_result["balance_remaining"])


@router.get("/tenant/info")
async def tenant_info(device_tenant=Depends(get_current_device), db: Session = Depends(get_db)):
    device, tenant = device_tenant
    return {
        "tenant_id": tenant.id, "name": tenant.name, "status": tenant.status,
        "max_whatsapp_accounts": tenant.max_whatsapp_accounts,
        "device_name": device.device_name, "whatsapp_phone": device.whatsapp_phone,
        "app_version": device.app_version,
    }
