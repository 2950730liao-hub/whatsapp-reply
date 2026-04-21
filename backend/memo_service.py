"""
销售备忘服务 - 记录客户沟通要点
"""
import sqlite3
from typing import List, Optional
from datetime import datetime


class MemoService:
    """销售备忘服务"""
    
    def __init__(self, db_path: str = "./data/memos.db"):
        self.db_path = db_path
        import os
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                tags TEXT,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()
    
    def create_memo(self, customer_id: int, content: str, 
                   category: str = "general", tags: str = "",
                   created_by: str = "") -> int:
        """创建备忘"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO memos (customer_id, content, category, tags, created_by)
            VALUES (?, ?, ?, ?, ?)
        """, (customer_id, content, category, tags, created_by))
        
        memo_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return memo_id
    
    def get_memos(self, customer_id: Optional[int] = None,
                  category: Optional[str] = None) -> List[dict]:
        """获取备忘列表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        query = "SELECT * FROM memos WHERE 1=1"
        params = []
        
        if customer_id:
            query += " AND customer_id = ?"
            params.append(customer_id)
        
        if category:
            query += " AND category = ?"
            params.append(category)
        
        query += " ORDER BY created_at DESC"
        
        cursor.execute(query, params)
        
        results = []
        for row in cursor.fetchall():
            results.append({
                "id": row[0],
                "customer_id": row[1],
                "content": row[2],
                "category": row[3],
                "tags": row[4],
                "created_by": row[5],
                "created_at": row[6]
            })
        
        conn.close()
        return results
    
    def update_memo(self, memo_id: int, content: str) -> bool:
        """更新备忘"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE memos 
            SET content = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (content, memo_id))
        
        conn.commit()
        conn.close()
        return True
    
    def delete_memo(self, memo_id: int) -> bool:
        """删除备忘"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM memos WHERE id = ?", (memo_id,))
        
        conn.commit()
        conn.close()
        return True
    
    def search_memos(self, keyword: str) -> List[dict]:
        """搜索备忘"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM memos 
            WHERE content LIKE ? OR tags LIKE ?
            ORDER BY created_at DESC
        """, (f"%{keyword}%", f"%{keyword}%"))
        
        results = []
        for row in cursor.fetchall():
            results.append({
                "id": row[0],
                "customer_id": row[1],
                "content": row[2],
                "category": row[3],
                "tags": row[4],
                "created_by": row[5],
                "created_at": row[6]
            })
        
        conn.close()
        return results


# 全局实例
_memo_service: Optional[MemoService] = None


def get_memo_service() -> MemoService:
    """获取备忘服务实例"""
    global _memo_service
    if _memo_service is None:
        _memo_service = MemoService()
    return _memo_service
