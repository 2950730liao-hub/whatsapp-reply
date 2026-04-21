"""
配置管理服务 - 安全存储API Key等敏感配置
"""
import os
import json
import sqlite3
import logging
from typing import Optional, Dict
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class ConfigService:
    """配置服务 - 加密存储敏感信息"""
    
    def __init__(self, db_path: str = "./data/config.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        # 生成或加载加密密钥
        self.key_file = "./data/.config_key"
        self.cipher = self._get_cipher()
        
        self._init_db()
    
    def _get_cipher(self) -> Fernet:
        """获取加密器"""
        if os.path.exists(self.key_file):
            with open(self.key_file, "rb") as f:
                key = f.read()
        else:
            key = Fernet.generate_key()
            with open(self.key_file, "wb") as f:
                f.write(key)
            # 设置文件权限，只允许所有者读写
            os.chmod(self.key_file, 0o600)
        
        return Fernet(key)
    
    def _init_db(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                is_encrypted INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()
    
    def set(self, key: str, value: str, encrypt: bool = True):
        """设置配置项"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if encrypt:
            value = self.cipher.encrypt(value.encode()).decode()
        
        cursor.execute("""
            INSERT OR REPLACE INTO config (key, value, is_encrypted, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """, (key, value, 1 if encrypt else 0))
        
        conn.commit()
        conn.close()
    
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """获取配置项"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT value, is_encrypted FROM config WHERE key = ?
        """, (key,))
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return default
        
        value, is_encrypted = row
        
        if is_encrypted:
            try:
                value = self.cipher.decrypt(value.encode()).decode()
            except InvalidToken:
                logger.warning(f"Failed to decrypt config value for key '{key}', returning default")
                return default
            except Exception as e:
                logger.error(f"Unexpected error decrypting config for key '{key}': {e}")
                raise
        
        return value
    
    def delete(self, key: str):
        """删除配置项"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM config WHERE key = ?", (key,))
        
        conn.commit()
        conn.close()
    
    def get_all(self) -> Dict[str, str]:
        """获取所有配置（敏感值隐藏）"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT key, value, is_encrypted FROM config
        """)
        
        results = {}
        for row in cursor.fetchall():
            key, value, is_encrypted = row
            if is_encrypted:
                # 敏感信息只显示部分
                results[key] = "********"
            else:
                results[key] = value
        
        conn.close()
        return results
    
    def set_llm_config(self, api_key: str, base_url: str, model: str):
        """设置LLM配置"""
        self.set("llm_api_key", api_key, encrypt=True)
        self.set("llm_base_url", base_url, encrypt=False)
        self.set("llm_model", model, encrypt=False)
    
    def get_llm_config(self) -> Dict[str, Optional[str]]:
        """获取LLM配置"""
        return {
            "api_key": self.get("llm_api_key"),
            "base_url": self.get("llm_base_url", "https://api.openai.com/v1"),
            "model": self.get("llm_model", "gpt-3.5-turbo")
        }


# 全局实例
_config_service: Optional[ConfigService] = None


def get_config_service() -> ConfigService:
    """获取配置服务实例"""
    global _config_service
    if _config_service is None:
        _config_service = ConfigService()
    return _config_service
