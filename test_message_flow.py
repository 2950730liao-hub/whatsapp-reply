#!/usr/bin/env python3
"""
测试消息接收和 AI 回复完整流程
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

import time
import requests
from database import SessionLocal, Customer, Message

def test_whatsapp_status():
    """测试 WhatsApp 连接状态"""
    print("=" * 60)
    print("📱 WhatsApp 连接状态测试")
    print("=" * 60)
    
    try:
        res = requests.get("http://localhost:8000/api/auth/status", timeout=5)
        data = res.json()
        
        print(f"后端类型: {data.get('backend', 'unknown')}")
        print(f"连接状态: {'✅ 已连接' if data.get('connected') else '❌ 未连接'}")
        print(f"登录状态: {'✅ 已登录' if data.get('logged_in') else '❌ 未登录'}")
        print(f"消息: {data.get('message', 'N/A')}")
        
        return data.get('connected') and data.get('logged_in')
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return False

def test_customer_count():
    """测试客户数量"""
    print("\n" + "=" * 60)
    print("👥 客户数据测试")
    print("=" * 60)
    
    db = SessionLocal()
    try:
        customers = db.query(Customer).all()
        print(f"客户总数: {len(customers)}")
        
        for c in customers[:5]:  # 只显示前5个
            print(f"  - {c.phone}: {c.name}")
        
        return len(customers)
    finally:
        db.close()

def test_message_count():
    """测试消息数量"""
    print("\n" + "=" * 60)
    print("💬 消息数据测试")
    print("=" * 60)
    
    db = SessionLocal()
    try:
        messages = db.query(Message).order_by(Message.created_at.desc()).limit(10).all()
        print(f"最近消息数: {len(messages)}")
        
        for m in messages:
            direction = "📤" if m.direction == "outgoing" else "📥"
            content = (m.content or "")[:30]
            print(f"  {direction} [{m.created_at.strftime('%H:%M:%S')}] {content}...")
        
        return len(messages)
    finally:
        db.close()

def test_send_message(phone: str, message: str):
    """测试发送消息"""
    print("\n" + "=" * 60)
    print(f"📤 发送消息测试 -> {phone}")
    print("=" * 60)
    
    # 先查找或创建客户
    db = SessionLocal()
    try:
        customer = db.query(Customer).filter(Customer.phone == phone).first()
        if not customer:
            print(f"创建新客户: {phone}")
            customer = Customer(phone=phone, name=phone)
            db.add(customer)
            db.commit()
            db.refresh(customer)
        
        customer_id = customer.id
        print(f"客户ID: {customer_id}")
    finally:
        db.close()
    
    # 发送消息
    try:
        res = requests.post(
            f"http://localhost:8000/api/customers/{customer_id}/messages",
            json={"content": message},
            timeout=30
        )
        data = res.json()
        
        if data.get("success"):
            print(f"✅ 消息发送成功")
            return True
        else:
            print(f"❌ 发送失败: {data.get('detail', '未知错误')}")
            return False
    except Exception as e:
        print(f"❌ 请求失败: {e}")
        return False

def wait_for_reply(phone: str, timeout: int = 60):
    """等待 AI 回复"""
    print("\n" + "=" * 60)
    print(f"⏳ 等待 AI 回复 ({timeout}秒)...")
    print("=" * 60)
    
    db = SessionLocal()
    try:
        # 获取当前最新消息ID
        last_msg = db.query(Message).order_by(Message.created_at.desc()).first()
        last_id = last_msg.id if last_msg else 0
        
        print(f"当前最新消息ID: {last_id}")
        print("请在手机上回复消息，或等待 AI 自动回复...")
        
        for i in range(timeout):
            time.sleep(1)
            
            # 检查新消息
            db.expire_all()  # 刷新缓存
            new_messages = db.query(Message).filter(
                Message.id > last_id,
                Message.direction == "outgoing"
            ).order_by(Message.created_at.desc()).all()
            
            if new_messages:
                print(f"\n✅ 收到 {len(new_messages)} 条新消息!")
                for m in new_messages:
                    content = (m.content or "")[:100]
                    print(f"  🤖 AI回复: {content}...")
                return True
            
            if i % 10 == 0:
                print(f"  已等待 {i} 秒...")
        
        print("\n⏰ 等待超时，未收到 AI 回复")
        return False
    finally:
        db.close()

def main():
    print("\n" + "🚀" * 30)
    print("WhatsApp 消息接收与 AI 回复测试")
    print("🚀" * 30 + "\n")
    
    # 1. 检查连接状态
    if not test_whatsapp_status():
        print("\n❌ WhatsApp 未连接，请先登录")
        return
    
    # 2. 检查客户数据
    customer_count = test_customer_count()
    
    # 3. 检查消息数据
    message_count = test_message_count()
    
    # 4. 发送测试消息
    test_phone = "8618028865868"  # 您的测试号码
    test_msg = "你好，这是一条测试消息，请回复"
    
    print("\n" + "-" * 60)
    print("准备发送测试消息...")
    print(f"目标号码: {test_phone}")
    print(f"消息内容: {test_msg}")
    print("-" * 60)
    
    input("\n按 Enter 键发送测试消息，或按 Ctrl+C 取消...")
    
    if test_send_message(test_phone, test_msg):
        # 5. 等待回复
        wait_for_reply(test_phone, timeout=60)
    
    print("\n" + "=" * 60)
    print("测试完成!")
    print("=" * 60)

if __name__ == "__main__":
    main()
