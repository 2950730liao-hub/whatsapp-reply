"""
Neonize 数据库监控服务
通过轮询监控通讯录变化，自动同步到 CRM
"""
import sqlite3
import hashlib
import logging
import threading
import time
from typing import Optional, Callable, List, Dict
from contact_sync_service import ContactSyncService, get_contact_sync_service

logger = logging.getLogger(__name__)

NEONIZE_DB_PATH = "/Users/liaoyujun/Documents/开发系统文件/whatsapp 机器人/backend/whatsapp_crm"


class ContactMonitorService:
    """通讯录数据库监控服务"""
    
    def __init__(self, db_path: str = NEONIZE_DB_PATH, poll_interval: int = 30):
        self.db_path = db_path
        self.poll_interval = poll_interval  # 轮询间隔（秒）
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._last_contacts_hash: Optional[str] = None
        self._on_contacts_changed: Optional[Callable] = None
        self._sync_service = get_contact_sync_service()
    
    def _get_contacts_hash(self) -> str:
        """计算当前通讯录数据的哈希值（用于检测变化）"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 获取所有联系人数据并计算哈希
            cursor.execute("""
                SELECT their_jid, push_name, full_name, first_name, business_name
                FROM whatsmeow_contacts
                ORDER BY their_jid
            """)
            
            contacts_data = cursor.fetchall()
            conn.close()
            
            # 计算哈希
            data_str = str(contacts_data)
            return hashlib.md5(data_str.encode()).hexdigest()
            
        except Exception as e:
            logger.error(f"[ContactMonitor] 计算哈希失败: {e}")
            return ""
    
    def _check_contacts_changed(self) -> bool:
        """检查通讯录是否发生变化"""
        current_hash = self._get_contacts_hash()
        
        if self._last_contacts_hash is None:
            # 首次运行
            self._last_contacts_hash = current_hash
            return False
        
        if current_hash != self._last_contacts_hash:
            logger.info("[ContactMonitor] 检测到通讯录变化")
            self._last_contacts_hash = current_hash
            return True
        
        return False
    
    def _monitor_loop(self):
        """监控循环"""
        logger.info(f"[ContactMonitor] 监控线程启动，轮询间隔: {self.poll_interval}秒")
        
        while self._running:
            try:
                # 检查通讯录变化
                if self._check_contacts_changed():
                    logger.info("[ContactMonitor] 通讯录发生变化，执行同步...")
                    
                    # 执行同步
                    stats = self._sync_service.sync_contacts_to_crm(remove_non_contacts=True)
                    logger.info(f"[ContactMonitor] 同步完成: {stats}")
                    
                    # 触发回调
                    if self._on_contacts_changed:
                        try:
                            self._on_contacts_changed(stats)
                        except Exception as e:
                            logger.error(f"[ContactMonitor] 回调执行失败: {e}")
                
                # 等待下次轮询
                time.sleep(self.poll_interval)
                
            except Exception as e:
                logger.error(f"[ContactMonitor] 监控循环错误: {e}")
                time.sleep(self.poll_interval)
        
        logger.info("[ContactMonitor] 监控线程已停止")
    
    def start(self):
        """启动监控"""
        if self._running:
            logger.warning("[ContactMonitor] 监控已在运行")
            return
        
        self._running = True
        
        # 初始化哈希值
        self._last_contacts_hash = self._get_contacts_hash()
        logger.info(f"[ContactMonitor] 初始通讯录哈希: {self._last_contacts_hash[:8]}...")
        
        # 启动监控线程
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        
        logger.info("[ContactMonitor] 监控服务已启动")
    
    def stop(self):
        """停止监控"""
        if not self._running:
            return
        
        self._running = False
        
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        
        logger.info("[ContactMonitor] 监控服务已停止")
    
    def force_sync(self) -> Dict:
        """强制立即同步"""
        logger.info("[ContactMonitor] 强制同步通讯录...")
        
        # 更新哈希值
        self._last_contacts_hash = self._get_contacts_hash()
        
        # 执行同步
        return self._sync_service.sync_contacts_to_crm(remove_non_contacts=True)
    
    def set_on_contacts_changed(self, callback: Callable):
        """设置通讯录变化回调"""
        self._on_contacts_changed = callback
    
    def get_contacts_summary(self) -> Dict:
        """获取通讯录摘要信息"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 统计联系人数量
            cursor.execute("SELECT COUNT(*) FROM whatsmeow_contacts")
            total = cursor.fetchone()[0]
            
            # 统计有名称的联系人
            cursor.execute("""
                SELECT COUNT(*) FROM whatsmeow_contacts 
                WHERE push_name IS NOT NULL AND push_name != ''
            """)
            with_name = cursor.fetchone()[0]
            
            conn.close()
            
            return {
                "total_contacts": total,
                "with_name": with_name,
                "without_name": total - with_name,
                "monitoring": self._running,
                "poll_interval": self.poll_interval
            }
            
        except Exception as e:
            logger.error(f"[ContactMonitor] 获取摘要失败: {e}")
            return {"error": str(e)}


# 全局服务实例
_contact_monitor_service: Optional[ContactMonitorService] = None


def get_contact_monitor_service() -> ContactMonitorService:
    """获取通讯录监控服务实例"""
    global _contact_monitor_service
    if _contact_monitor_service is None:
        _contact_monitor_service = ContactMonitorService()
    return _contact_monitor_service


if __name__ == "__main__":
    # 测试监控服务
    monitor = ContactMonitorService(poll_interval=10)
    
    # 设置变化回调
    def on_changed(stats):
        print(f"通讯录变化！同步结果: {stats}")
    
    monitor.set_on_contacts_changed(on_changed)
    
    # 启动监控
    monitor.start()
    
    print("监控已启动，按 Ctrl+C 停止...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n停止监控...")
        monitor.stop()
