"""
修复重复客户数据脚本
将 LID 客户合并到手机号客户
"""
import sqlite3
import sys

def fix_duplicate_customers():
    """修复重复客户：将 LID 客户合并到手机号客户"""
    
    DB_PATH = "/Users/liaoyujun/Documents/开发系统文件/whatsapp 机器人/backend/data/whatsapp_crm.db"
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    print("=" * 70)
    print("🔧 修复重复客户数据")
    print("=" * 70)
    
    # 1. 查找所有客户
    cursor.execute("SELECT id, phone, name FROM customers")
    customers = cursor.fetchall()
    
    print(f"\n📊 当前共有 {len(customers)} 个客户:")
    for c in customers:
        print(f"   ID:{c[0]} Phone:{c[1]} Name:{c[2]}")
    
    # 2. 查找 LID 映射关系
    NEONIZE_DB = "/Users/liaoyujun/Documents/开发系统文件/whatsapp 机器人/backend/whatsapp_crm"
    neonize_conn = sqlite3.connect(NEONIZE_DB)
    neonize_cursor = neonize_conn.cursor()
    
    neonize_cursor.execute("SELECT lid, pn FROM whatsmeow_lid_map")
    lid_mappings = {row[0]: row[1] for row in neonize_cursor.fetchall()}
    neonize_conn.close()
    
    print(f"\n🔗 LID 映射关系:")
    for lid, pn in lid_mappings.items():
        print(f"   {lid} -> {pn}")
    
    # 3. 识别需要合并的客户
    # 查找 phone 是 LID 的客户
    lid_customers = []
    phone_customers = {}
    
    for c in customers:
        customer_id, phone, name = c
        if phone in lid_mappings:
            # 这是一个 LID 客户
            lid_customers.append({
                'id': customer_id,
                'lid': phone,
                'phone': lid_mappings[phone],
                'name': name
            })
        else:
            # 这是一个正常手机号客户
            phone_customers[phone] = {
                'id': customer_id,
                'phone': phone,
                'name': name
            }
    
    print(f"\n📋 分析结果:")
    print(f"   LID 客户: {len(lid_customers)} 个")
    print(f"   手机号客户: {len(phone_customers)} 个")
    
    # 4. 执行合并
    merged_count = 0
    for lid_customer in lid_customers:
        lid_id = lid_customer['id']
        lid = lid_customer['lid']
        phone = lid_customer['phone']
        
        # 查找对应的手机号客户
        if phone in phone_customers:
            phone_customer = phone_customers[phone]
            phone_id = phone_customer['id']
            
            print(f"\n🔄 合并客户:")
            print(f"   LID 客户: ID={lid_id}, Phone={lid}")
            print(f"   -> 手机号客户: ID={phone_id}, Phone={phone}")
            
            # 更新消息关联
            cursor.execute(
                "UPDATE messages SET customer_id = ? WHERE customer_id = ?",
                (phone_id, lid_id)
            )
            msg_updated = cursor.rowcount
            print(f"   更新了 {msg_updated} 条消息")
            
            # 更新会话关联
            cursor.execute(
                "UPDATE conversations SET customer_id = ? WHERE customer_id = ?",
                (phone_id, lid_id)
            )
            conv_updated = cursor.rowcount
            print(f"   更新了 {conv_updated} 条会话")
            
            # 删除 LID 客户
            cursor.execute("DELETE FROM customers WHERE id = ?", (lid_id,))
            print(f"   删除了 LID 客户 ID={lid_id}")
            
            merged_count += 1
        else:
            # 没有对应的手机号客户，直接更新 LID 为手机号
            print(f"\n📝 更新 LID 客户为手机号:")
            print(f"   ID={lid_id}: {lid} -> {phone}")
            cursor.execute(
                "UPDATE customers SET phone = ? WHERE id = ?",
                (phone, lid_id)
            )
    
    # 5. 提交更改
    conn.commit()
    
    # 6. 验证结果
    cursor.execute("SELECT id, phone, name FROM customers ORDER BY id")
    final_customers = cursor.fetchall()
    
    print(f"\n" + "=" * 70)
    print("✅ 修复完成")
    print("=" * 70)
    print(f"合并/更新了 {merged_count} 个客户")
    print(f"\n📊 修复后共有 {len(final_customers)} 个客户:")
    for c in final_customers:
        print(f"   ID:{c[0]} Phone:{c[1]} Name:{c[2]}")
    
    # 7. 验证消息
    cursor.execute("""
        SELECT c.phone, COUNT(m.id) as msg_count
        FROM customers c
        LEFT JOIN messages m ON c.id = m.customer_id
        GROUP BY c.id
    """)
    msg_stats = cursor.fetchall()
    print(f"\n💬 消息统计:")
    for phone, count in msg_stats:
        print(f"   {phone}: {count} 条消息")
    
    conn.close()
    
    print("\n" + "=" * 70)
    print("🎉 数据修复完成！")
    print("=" * 70)

if __name__ == "__main__":
    fix_duplicate_customers()
