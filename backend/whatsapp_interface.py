"""WhatsApp 客户端统一接口定义"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, Callable, List, Dict, Any

logger = logging.getLogger(__name__)


class IWhatsAppClient(ABC):
    """WhatsApp 客户端统一接口
    
    所有 WhatsApp 后端实现（Neonize、CLI、Baileys 等）必须实现此接口。
    """
    
    @abstractmethod
    def connect(self) -> bool:
        """连接 WhatsApp 服务"""
        pass
    
    @abstractmethod
    def disconnect(self) -> None:
        """断开连接"""
        pass
    
    @abstractmethod
    def is_connected(self) -> bool:
        """检查当前连接状态"""
        pass
    
    @abstractmethod
    def send_message(self, jid: str, message: str, **kwargs) -> bool:
        """发送消息
        
        Args:
            jid: 接收者的 JID
            message: 消息内容
        
        Returns:
            是否发送成功
        """
        pass
    
    @abstractmethod
    def on_message(self, handler: Callable) -> None:
        """注册消息接收处理器
        
        Args:
            handler: 消息处理回调函数
        """
        pass
    
    @abstractmethod
    def get_current_user(self) -> Optional[str]:
        """获取当前登录用户信息"""
        pass
    
    @abstractmethod
    def get_qr_code(self) -> Optional[str]:
        """获取登录二维码"""
        pass
    
    @property
    @abstractmethod
    def backend_name(self) -> str:
        """后端名称标识"""
        pass
    
    @abstractmethod
    def is_authenticated(self) -> bool:
        """检查是否已通过 WhatsApp 认证"""
        pass
    
    def get_contacts(self) -> list:
        """获取联系人列表（可选实现）"""
        return []
    
    def get_chats(self) -> list:
        """获取聊天列表（可选实现）"""
        return []
    
    def logout(self) -> bool:
        """退出登录（可选实现）"""
        logger.warning(f"当前后端不支持 logout 操作")
        return False
    
    def auth_status(self) -> dict:
        """获取认证状态详情（可选实现）"""
        return {"authenticated": self.is_authenticated()}
