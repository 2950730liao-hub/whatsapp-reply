"""
WhatsApp 客户端适配器
自动选择最佳方案：Neonize (优先) > CLI (备用)
"""
import os

# 尝试导入 Neonize
try:
    from neonize_client import NeonizeWhatsAppClient, MessageSyncer, init_whatsapp_client, get_whatsapp_client
    NEONIZE_AVAILABLE = True
    print("[WhatsApp Adapter] 使用 Neonize 方案")
except ImportError as e:
    NEONIZE_AVAILABLE = False
    print(f"[WhatsApp Adapter] Neonize 不可用: {e}")
    print("[WhatsApp Adapter] 回退到 CLI 方案")
    from whatsapp_client import WhatsAppClient, MessageSyncer
    
    # 创建兼容的初始化函数
    def init_whatsapp_client():
        return WhatsAppClient()
    
    def get_whatsapp_client():
        # 全局变量需要在 main.py 中定义
        import main
        return main.whatsapp_client


# 统一的客户端类
class WhatsAppClientAdapter:
    """WhatsApp 客户端适配器"""
    
    def __init__(self):
        self._client = None
        self._use_neonize = NEONIZE_AVAILABLE
    
    def init(self):
        """初始化客户端"""
        if self._use_neonize:
            self._client = init_whatsapp_client()
        else:
            from whatsapp_client import WhatsAppClient
            self._client = WhatsAppClient()
        return self._client
    
    def get_client(self):
        """获取客户端实例"""
        return self._client
    
    def is_using_neonize(self) -> bool:
        """是否使用 Neonize"""
        return self._use_neonize


# 全局适配器
_adapter = WhatsAppClientAdapter()

def get_adapter() -> WhatsAppClientAdapter:
    """获取适配器"""
    return _adapter
