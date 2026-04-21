#!/usr/bin/env python3
"""
测试自动回复功能
模拟接收消息并检查是否触发自动回复
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

from database import SessionLocal, Customer, Message, Conversation
from communication_service import CommunicationService
from neonize_client import NeonizeWhatsAppClient, get_current_qr

def test_auto_reply():
    """测试自动回复功能"""
    print("=" * 60)
    print("测试自动回复功能")
    print("=" * 60)
    
    # 创建数据库会话
    db = SessionLocal()
    
    try:
        # 查找测试客户（1794）
        customer = db.query(Customer).filter(
            Customer.phone == "8618026251794"
        ).first()
        
        if not customer:
            print("❌ 未找到测试客户 8618026251794")
            return
        
        print(f"✅ 找到客户: {customer.phone} (ID: {customer.id})")
        print(f"   分类: {customer.category}")
        
        # 检查现有消息
        existing_messages = db.query(Message).filter(
            Message.customer_id == customer.id
        ).order_by(Message.created_at.desc()).all()
        
        print(f"\n📨 现有消息数: {len(existing_messages)}")
        for msg in existing_messages[:3]:
            direction = "发" if msg.direction == "outgoing" else "收"
            print(f"   [{direction}] {msg.content[:40]}...")
        
        # 创建模拟接收消息
        print("\n📝 创建模拟接收消息...")
        test_message = Message(
            customer_id=customer.id,
            content="你好，我想咨询一下产品",
            direction="incoming",
            sender_name=customer.phone,
            message_type="text"
        )
        db.add(test_message)
        db.commit()
        db.refresh(test_message)
        print(f"✅ 模拟消息已创建 (ID: {test_message.id})")
        
        # 初始化 WhatsApp 客户端（用于发送回复）
        print("\n🔌 初始化 WhatsApp 客户端...")
        wa_client = NeonizeWhatsAppClient("test_session")
        
        # 创建通信服务
        comm_service = CommunicationService(db, wa_client)
        
        # 处理消息（触发自动回复）
        print("\n🤖 触发自动回复逻辑...")
        print("-" * 60)
        need_notify = comm_service.handle_incoming_message(test_message, customer)
        print("-" * 60)
        
        print(f"\n📢 需要通知人工: {need_notify}")
        
        # 检查是否生成了回复消息
        db.refresh(test_message)
        new_messages = db.query(Message).filter(
            Message.customer_id == customer.id,
            Message.created_at > test_message.created_at
        ).all()
        
        print(f"\n📤 新发送的消息数: {len(new_messages)}")
        for msg in new_messages:
            print(f"   [发送] {msg.content[:60]}...")
        
        if new_messages:
            print("\n✅ 自动回复功能正常！")
        else:
            print("\n⚠️  未检测到自动回复消息")
            print("   可能原因：")
            print("   1. 已经发送过欢迎语（新客户只发一次）")
            print("   2. 会话状态为 handover（人工接管）")
            print("   3. AI 回复生成失败")
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        db.close()

if __name__ == "__main__":
    test_auto_reply()
