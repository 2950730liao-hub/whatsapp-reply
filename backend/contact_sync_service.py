"""
通讯录同步服务
从 Neonize 数据库同步真实联系人到 CRM
支持多号码隔离和自动同步
"""
import sqlite3
import logging
from typing import List, Dict, Optional, Set
from database import SessionLocal, Customer

logger = logging.getLogger(__name__)

NEONIZE_DB_PATH = "/Users/liaoyujun/Documents/开发系统文件/whatsapp 机器人/backend/whatsapp_crm"


class ContactSyncService:
    """通讯录同步服务"""
    
    def __init__(self, neonize_db_path: str = NEONIZE_DB_PATH):
        self.neonize_db_path = neonize_db_path
    
    def _get_neonize_contacts(self) -> List[Dict]:
        """从 Neonize 数据库获取联系人"""
        contacts = []
        try:
            conn = sqlite3.connect(self.neonize_db_path)
            cursor = conn.cursor()
            
            # 获取所有联系人（包括LID格式）
            cursor.execute("""
                SELECT 
                    c.their_jid,
                    c.first_name,
                    c.full_name,
                    c.push_name,
                    c.business_name
                FROM whatsmeow_contacts c
            """)
            
            for row in cursor.fetchall():
                jid = row[0]
                # 提取手机号
                if '@s.whatsapp.net' in jid:
                    phone = jid.split('@')[0]
                elif '@lid' in jid:
                    # LID格式，查询映射表
                    lid = jid.split('@')[0]
                    cursor.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (lid,))
                    lid_row = cursor.fetchone()
                    phone = lid_row[0] if lid_row else lid
                else:
                    phone = jid
                
                contacts.append({
                    'jid': jid,
                    'first_name': row[1],
                    'full_name': row[2],
                    'push_name': row[3],
                    'business_name': row[4],
                    'phone_number': phone
                })
            
            conn.close()
            logger.info(f"[ContactSync] 从 Neonize 获取 {len(contacts)} 个联系人")
            return contacts
            
        except Exception as e:
            logger.error(f"[ContactSync] 获取联系人失败: {e}")
            return []
    
    def _extract_phone_from_jid(self, jid: str) -> str:
        """从 JID 提取手机号"""
        if '@' in jid:
            return jid.split('@')[0]
        return jid
    
    def _get_display_name(self, contact: Dict) -> str:
        """获取显示名称（按优先级）"""
        return (
            contact.get('push_name') or 
            contact.get('full_name') or 
            contact.get('first_name') or 
            contact.get('business_name') or 
            contact['phone_number']
        )
    
    def get_current_login_number(self) -> Optional[str]:
        """获取当前登录的 WhatsApp 号码"""
        try:
            conn = sqlite3.connect(self.neonize_db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT jid FROM whatsmeow_device LIMIT 1")
            row = cursor.fetchone()
            conn.close()
            
            if row:
                device_jid = row[0]
                # 格式: 8618026251794:6@s.whatsapp.net
                return device_jid.split(':')[0]
        except Exception as e:
            logger.error(f"[ContactSync] 获取登录号码失败: {e}")
        return None
    
    def _is_self(self, phone: str) -> bool:
        """检查是否是自己（登录号码）"""
        current = self.get_current_login_number()
        return phone == current if current else False
    
    def _get_contacts_phone_set(self) -> Set[str]:
        """获取通讯录中所有手机号集合（用于对比）"""
        contacts = self._get_neonize_contacts()
        phones = set()
        for contact in contacts:
            phone = contact['phone_number']
            if not self._is_self(phone):
                phones.add(phone)
        return phones
    
    def sync_contacts_to_crm(self, remove_non_contacts: bool = True) -> Dict:
        """
        同步联系人到 CRM
        
        Args:
            remove_non_contacts: 是否删除不在通讯录中的客户
        
        返回统计信息
        """
        stats = {
            'login_number': self.get_current_login_number(),
            'total_contacts': 0,
            'new_customers': 0,
            'updated_customers': 0,
            'skipped_self': 0,
            'removed_customers': 0,
            'errors': 0
        }
        
        # 获取当前登录号码
        current_login = self.get_current_login_number()
        if not current_login:
            logger.error("[ContactSync] 无法获取当前登录号码，同步取消")
            stats['errors'] += 1
            return stats
        
        logger.info(f"[ContactSync] 当前登录号码: {current_login}")
        
        contacts = self._get_neonize_contacts()
        stats['total_contacts'] = len(contacts)
        
        # 获取通讯录手机号集合
        contact_phones = self._get_contacts_phone_set()
        
        db = SessionLocal()
        try:
            # 第一步：清理不在通讯录中的客户
            if remove_non_contacts:
                all_customers = db.query(Customer).all()
                for customer in all_customers:
                    if customer.phone not in contact_phones:
                        logger.info(f"[ContactSync] 删除非通讯录客户: {customer.phone} - {customer.name}")
                        db.delete(customer)
                        stats['removed_customers'] += 1
                
                if stats['removed_customers'] > 0:
                    db.commit()
                    logger.info(f"[ContactSync] 已清理 {stats['removed_customers']} 个非通讯录客户")
            
            # 第二步：同步通讯录客户
            for contact in contacts:
                try:
                    phone = contact['phone_number']
                    
                    # 跳过自己
                    if self._is_self(phone):
                        stats['skipped_self'] += 1
                        logger.info(f"[ContactSync] 跳过自己: {phone}")
                        continue
                    
                    # 检查是否已存在
                    existing = db.query(Customer).filter(
                        Customer.phone == phone
                    ).first()
                    
                    display_name = self._get_display_name(contact)
                    
                    if existing:
                        # 更新现有客户 - 优先使用通讯录名称
                        should_update = False
                        
                        # 如果当前名称是默认生成的（包含"客户"或是手机号），则更新
                        if not existing.name or existing.name == phone or '客户' in existing.name:
                            should_update = True
                        # 如果通讯录有名称且当前名称不同，也更新
                        elif display_name and display_name != phone and existing.name != display_name:
                            should_update = True
                        
                        if should_update:
                            old_name = existing.name
                            existing.name = display_name
                            stats['updated_customers'] += 1
                            logger.info(f"[ContactSync] 更新客户名称: {phone} '{old_name}' -> '{display_name}'")
                    else:
                        # 创建新客户
                        customer = Customer(
                            phone=phone,
                            name=display_name,
                            category="new",  # 默认分类
                            status="active"
                        )
                        db.add(customer)
                        stats['new_customers'] += 1
                        logger.info(f"[ContactSync] 创建新客户: {phone} - {display_name}")
                
                except Exception as e:
                    stats['errors'] += 1
                    logger.error(f"[ContactSync] 处理联系人失败 {contact}: {e}")
            
            db.commit()
            logger.info(f"[ContactSync] 同步完成: {stats}")
            return stats
            
        except Exception as e:
            db.rollback()
            logger.error(f"[ContactSync] 同步失败: {e}")
            raise
        finally:
            db.close()
    
    def auto_sync_on_login(self):
        """登录后自动同步通讯录"""
        logger.info("[ContactSync] 登录后自动同步通讯录...")
        return self.sync_contacts_to_crm(remove_non_contacts=True)
    
    def get_contact_info(self, phone: str) -> Optional[Dict]:
        """获取单个联系人信息"""
        contacts = self._get_neonize_contacts()
        for contact in contacts:
            if contact['phone_number'] == phone:
                return {
                    'phone': phone,
                    'name': self._get_display_name(contact),
                    'jid': contact['jid'],
                    'business_name': contact.get('business_name')
                }
        return None


# 全局服务实例
_contact_sync_service: Optional[ContactSyncService] = None


def get_contact_sync_service() -> ContactSyncService:
    """获取通讯录同步服务实例"""
    global _contact_sync_service
    if _contact_sync_service is None:
        _contact_sync_service = ContactSyncService()
    return _contact_sync_service


if __name__ == "__main__":
    # 测试同步
    service = ContactSyncService()
    stats = service.sync_contacts_to_crm()
    print(f"同步结果: {stats}")
