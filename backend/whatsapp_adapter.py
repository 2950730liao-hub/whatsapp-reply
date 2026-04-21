"""
WhatsApp 客户端适配器
统一管理不同后端（Neonize/CLI）的生命周期
"""
import os
import asyncio
import threading
import logging
import subprocess
from typing import Optional, Callable

from whatsapp_interface import IWhatsAppClient

logger = logging.getLogger(__name__)


class WhatsAppClientManager:
    """WhatsApp 客户端统一管理器
    
    封装不同后端的生命周期管理，通过环境变量选择后端。
    """
    
    def __init__(self, backend: Optional[str] = None):
        self.backend = backend or os.getenv("WHATSAPP_BACKEND", "neonize").lower()
        self._client: Optional[IWhatsAppClient] = None
        self._message_handler: Optional[Callable] = None
        self._message_syncer = None
        self._client_ready = threading.Event()
        self._client_lock = threading.Lock()
        logger.info(f"[WhatsAppClientManager] 初始化，后端: {self.backend}")
    
    def create_client(self) -> IWhatsAppClient:
        """创建客户端实例（不启动连接）"""
        if self.backend == "neonize":
            from neonize_client import NeonizeWhatsAppClient, set_client_ready_event
            self._client = NeonizeWhatsAppClient()
            # 设置就绪事件回调
            set_client_ready_event(self._client_ready)
        elif self.backend == "cli":
            from whatsapp_client import WhatsAppClient
            self._client = WhatsAppClient()
        else:
            raise ValueError(f"不支持的 WhatsApp 后端: {self.backend}")
        return self._client
    
    def initialize(self) -> IWhatsAppClient:
        """创建并初始化客户端（包括连接）
        
        封装不同后端的启动逻辑差异：
        - Neonize：线程启动 + 事件驱动
        - CLI：子进程 + 轮询
        """
        with self._client_lock:
            if self._client is None:
                self.create_client()
            
            if self.backend == "neonize":
                self._initialize_neonize()
            elif self.backend == "cli":
                self._initialize_cli()
        
        return self._client
    
    def _initialize_neonize(self):
        """初始化 Neonize 后端"""
        logger.info("🚀 启动 Neonize WhatsApp 客户端...")
        
        # 启动客户端连接（后台线程）
        client_thread = threading.Thread(
            target=self._client.connect_with_retry,
            daemon=True
        )
        client_thread.start()
        logger.info("✅ Neonize 客户端已在后台启动")
        logger.info("⚠️  首次使用请扫描二维码登录 WhatsApp")
        
        # 等待客户端初始化完成
        self._client_ready.wait(timeout=5)
        logger.info("✅ 客户端初始化完成信号已接收")
    
    def _initialize_cli(self):
        """初始化 CLI 后端"""
        if not self._client.is_authenticated():
            logger.warning("⚠️  WhatsApp 未登录，请运行: whatsapp auth login")
        else:
            logger.info("✅ WhatsApp 已连接")
            
            # 启动 sync --follow 后台进程保持连接
            subprocess.Popen(
                ["python3", "whatsapp_sync_manager.py"],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                stdout=open("/tmp/sync_manager.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
            logger.info("✅ WhatsApp Sync 管理器已启动")
            
            # 启动消息同步
            from whatsapp_client import MessageSyncer
            self._message_syncer = MessageSyncer(self._client)
            asyncio.create_task(self._message_syncer.start_polling(interval=1))
            logger.info("✅ 消息同步已启动")
    
    def register_message_handler(self, handler: Callable):
        """注册消息处理器"""
        self._message_handler = handler
        if self._client:
            if self.backend == "neonize":
                self._client.on_message(handler)
            # CLI 模式下，处理器在 MessageSyncer 中调用，无需额外注册
    
    def shutdown(self):
        """统一关闭流程"""
        with self._client_lock:
            # 1. 停止消息同步器
            if self._message_syncer:
                try:
                    self._message_syncer.stop()
                    logger.info("消息同步器已停止")
                except Exception as e:
                    logger.error(f"停止消息同步器失败: {e}")
            
            # 2. 断开客户端连接
            if self._client:
                try:
                    self._client.disconnect()
                    logger.info("WhatsApp 客户端已断开")
                except Exception as e:
                    logger.error(f"断开 WhatsApp 客户端失败: {e}")
    
    @property
    def client(self) -> Optional[IWhatsAppClient]:
        """获取客户端实例"""
        return self._client
    
    @property
    def message_syncer(self):
        """获取消息同步器（CLI 模式下可用）"""
        return self._message_syncer
    
    @property
    def is_neonize(self) -> bool:
        """是否使用 Neonize 后端"""
        return self.backend == "neonize"
    
    @property
    def is_cli(self) -> bool:
        """是否使用 CLI 后端"""
        return self.backend == "cli"
    
    def get_backend_name(self) -> str:
        """获取后端名称"""
        return self.backend


# 全局管理器实例（向后兼容）
_client_manager: Optional[WhatsAppClientManager] = None


def get_client_manager() -> Optional[WhatsAppClientManager]:
    """获取全局客户端管理器实例"""
    global _client_manager
    return _client_manager


def create_client_manager(backend: Optional[str] = None) -> WhatsAppClientManager:
    """创建新的客户端管理器实例"""
    global _client_manager
    _client_manager = WhatsAppClientManager(backend)
    return _client_manager


# 向后兼容的别名
WhatsAppClientAdapter = WhatsAppClientManager


def get_adapter() -> WhatsAppClientManager:
    """获取适配器（向后兼容）"""
    return get_client_manager()
