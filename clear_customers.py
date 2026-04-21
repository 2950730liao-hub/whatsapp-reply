#!/usr/bin/env python3
"""
清空所有客户数据
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

from database import SessionLocal, Customer, Message, Conversation

def clear_all_customers():
    """删除所有客户及相关数据"""
    db = SessionLocal()
    try:
        # 获取客户数量
        customer_count = db.query(Customer).count()
        message_count = db.query(Message).count()
        conversation_count = db.query(Conversation).count()
        
        print(f"当前数据：")
        print(f"  客户数: {customer_count}")
        print(f"  消息数: {message_count}")
        print(f"  会话数: {conversation_count}")
        
        # 删除所有消息
        db.query(Message).delete()
        print("✅ 已删除所有消息")
        
        # 删除所有会话
        db.query(Conversation).delete()
        print("✅ 已删除所有会话")
        
        # 删除所有客户
        db.query(Customer).delete()
        print("✅ 已删除所有客户")
        
        db.commit()
        print("\n🎉 所有客户数据已清空！")
        
    except Exception as e:
        print(f"❌ 删除失败: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    confirm = input("确定要删除所有客户数据吗？此操作不可恢复！(yes/no): ")
    if confirm.lower() == 'yes':
        clear_all_customers()
    else:
        print("已取消操作")
