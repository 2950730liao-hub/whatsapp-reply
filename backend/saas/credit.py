"""
算力计费服务
扣减、充值、查询
"""
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session
from fastapi import HTTPException

# 计费规则：1积分 = 1000 tokens
CREDITS_PER_1K_TOKENS = 1.0


def get_or_create_credit_account(tenant_id: int, db: Session):
    """获取或创建算力账户"""
    from saas.models import CreditAccount
    account = db.query(CreditAccount).filter(CreditAccount.tenant_id == tenant_id).first()
    if not account:
        account = CreditAccount(tenant_id=tenant_id, balance=0.0)
        db.add(account)
        db.commit()
        db.refresh(account)
    return account


def check_and_deduct_credits(
    tenant_id: int,
    db: Session,
    tokens_used: int = 0,
    description: str = "AI回复",
    agent_id: Optional[int] = None,
    customer_phone: Optional[str] = None,
) -> dict:
    """
    检查并扣减算力
    按次计费：每次AI回复消耗1积分
    按Token计费：每1000 token消耗1积分（如果有token数据则按token）
    """
    from saas.models import CreditAccount, CreditTransaction
    
    account = get_or_create_credit_account(tenant_id, db)
    
    # 计算消耗（按token或按次）
    if tokens_used > 0:
        consumed = round(tokens_used / 1000.0, 4)
    else:
        consumed = 1.0  # 按次计费，默认1积分/次
    
    if account.balance < consumed:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "INSUFFICIENT_CREDIT",
                "message": f"算力不足，当前余额 {account.balance:.2f}，需要 {consumed:.2f}",
                "balance": account.balance,
                "required": consumed,
            }
        )
    
    # 扣减余额
    account.balance = round(account.balance - consumed, 4)
    account.total_consumed = round(account.total_consumed + consumed, 4)
    
    # 记录交易
    tx = CreditTransaction(
        tenant_id=tenant_id,
        type="consume",
        amount=-consumed,
        balance_after=account.balance,
        description=description,
        tokens_used=tokens_used,
        agent_id=agent_id,
        customer_phone=customer_phone,
    )
    db.add(tx)
    db.commit()
    
    return {
        "credit_consumed": consumed,
        "balance_remaining": account.balance,
        "tokens_used": tokens_used,
    }


def recharge_credits(
    tenant_id: int,
    amount: float,
    db: Session,
    order_id: Optional[str] = None,
    description: str = "手动充值",
) -> dict:
    """充值算力"""
    from saas.models import CreditAccount, CreditTransaction
    
    if amount <= 0:
        raise HTTPException(status_code=400, detail="充值金额必须大于0")
    
    account = get_or_create_credit_account(tenant_id, db)
    account.balance = round(account.balance + amount, 4)
    account.total_recharged = round(account.total_recharged + amount, 4)
    
    tx = CreditTransaction(
        tenant_id=tenant_id,
        type="recharge",
        amount=amount,
        balance_after=account.balance,
        description=description,
        recharge_order_id=order_id,
    )
    db.add(tx)
    db.commit()
    
    return {
        "recharged": amount,
        "balance": account.balance,
        "total_recharged": account.total_recharged,
    }


def get_balance(tenant_id: int, db: Session) -> dict:
    """查询余额"""
    account = get_or_create_credit_account(tenant_id, db)
    is_low = account.balance <= account.alert_threshold
    return {
        "balance": account.balance,
        "total_recharged": account.total_recharged,
        "total_consumed": account.total_consumed,
        "alert_threshold": account.alert_threshold,
        "is_low_balance": is_low,
    }
