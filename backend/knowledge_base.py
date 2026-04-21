"""
知识库系统 - 文档管理和向量检索
"""
import os
import sqlite3
import hashlib
from typing import List, Optional
from datetime import datetime


class KnowledgeBase:
    """知识库管理"""
    
    def __init__(self, db_path: str = "./data/knowledge_base.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 文档表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                doc_type TEXT DEFAULT 'text',
                category TEXT DEFAULT 'general',
                file_hash TEXT UNIQUE,
                file_path TEXT,
                file_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 兼容旧数据库：新增列（如果不存在）
        for col, col_def in [("file_path", "TEXT"), ("file_url", "TEXT")]:
            try:
                cursor.execute(f"ALTER TABLE documents ADD COLUMN {col} {col_def}")
            except Exception:
                pass  # 列已存在，忽略
        
        # 关键词索引表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id INTEGER,
                keyword TEXT,
                FOREIGN KEY (doc_id) REFERENCES documents(id)
            )
        """)
        
        # 创建索引以优化搜索性能
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_keywords_keyword ON keywords(keyword)
        """)
        
        # 附件表 - 支持一个文档多个附件
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_url TEXT NOT NULL,
                file_type TEXT DEFAULT 'image',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
            )
        """)
        
        # 兼容旧数据库：添加 name 列（如果不存在）
        try:
            cursor.execute("ALTER TABLE attachments ADD COLUMN name TEXT")
            # 为现有数据设置默认值
            cursor.execute("UPDATE attachments SET name = file_name WHERE name IS NULL")
        except Exception:
            pass  # 列已存在，忽略
        
        # 附件表索引
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_attachments_doc_id ON attachments(doc_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_keywords_doc_id ON keywords(doc_id)
        """)
        
        conn.commit()
        conn.close()
    
    def add_document(self, title: str, content: str, 
                    doc_type: str = "text", category: str = "general",
                    file_path: str = None, file_url: str = None) -> int:
        """添加文档"""
        # 计算内容哈希
        file_hash = hashlib.md5(content.encode()).hexdigest()
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                INSERT INTO documents (title, content, doc_type, category, file_hash, file_path, file_url)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (title, content, doc_type, category, file_hash, file_path, file_url))
            
            doc_id = cursor.lastrowid
            
            # 提取关键词并建立索引
            keywords = self._extract_keywords(content)
            for keyword in keywords:
                cursor.execute("""
                    INSERT INTO keywords (doc_id, keyword)
                    VALUES (?, ?)
                """, (doc_id, keyword))
            
            conn.commit()
            return doc_id
            
        except sqlite3.IntegrityError:
            # 文档已存在
            cursor.execute("SELECT id FROM documents WHERE file_hash = ?", (file_hash,))
            result = cursor.fetchone()
            return result[0] if result else 0
        finally:
            conn.close()
    
    def add_file_document(self, title: str, file_path: str, file_url: str,
                          content: str = "", doc_type: str = "file",
                          category: str = "general") -> int:
        """添加文件型文档（图片/PDF/Word）"""
        return self.add_document(
            title=title,
            content=content or f"[{doc_type}文件] {title}",
            doc_type=doc_type,
            category=category,
            file_path=file_path,
            file_url=file_url
        )
    
    def search_documents(self, query: str, limit: int = 5) -> List[dict]:
        """搜索文档"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 简单关键词匹配
        keywords = self._extract_keywords(query)
        
        if not keywords:
            # 返回最新文档
            cursor.execute("""
                SELECT id, title, content, category, created_at
                FROM documents
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,))
        else:
            # 关键词匹配
            placeholders = ','.join(['?' for _ in keywords])
            cursor.execute(f"""
                SELECT d.id, d.title, d.content, d.category, d.created_at,
                       COUNT(k.id) as match_count
                FROM documents d
                LEFT JOIN keywords k ON d.id = k.doc_id
                WHERE k.keyword IN ({placeholders})
                GROUP BY d.id
                ORDER BY match_count DESC, d.created_at DESC
                LIMIT ?
            """, keywords + [limit])
        
        results = []
        for row in cursor.fetchall():
            results.append({
                "id": row[0],
                "title": row[1],
                "content": row[2][:500] + "..." if len(row[2]) > 500 else row[2],
                "category": row[3],
                "created_at": row[4]
            })
        
        conn.close()
        return results
    
    def get_relevant_knowledge(self, query: str, category: Optional[str] = None) -> str:
        """获取相关知识（包含附件信息）"""
        docs = self.search_documents(query, limit=3)
        
        # 同时搜索标题、内容、附件名称匹配
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 搜索文档标题和内容
        cursor.execute("""
            SELECT id, title, content FROM documents
            WHERE title LIKE ? OR content LIKE ?
        """, (f'%{query}%', f'%{query}%'))
        title_matches = cursor.fetchall()
        
        # 搜索附件名称匹配
        cursor.execute("""
            SELECT DISTINCT doc_id FROM attachments
            WHERE name LIKE ?
        """, (f'%{query}%',))
        attachment_matches = cursor.fetchall()
        
        conn.close()
        
        # 合并结果，去重
        all_doc_ids = set()
        for doc in docs:
            all_doc_ids.add(doc['id'])
        for row in title_matches:
            all_doc_ids.add(row[0])
        for row in attachment_matches:
            all_doc_ids.add(row[0])
        
        if not all_doc_ids:
            return ""
        
        knowledge = "相关知识点：\n"
        for doc_id in list(all_doc_ids)[:3]:
            doc = self.get_document_by_id(doc_id)
            if doc:
                content_preview = doc['content'][:200] if len(doc['content']) > 200 else doc['content']
                knowledge += f"- {doc['title']}: {content_preview}\n"
                
                # 添加附件信息
                attachments = self.get_attachments(doc_id)
                if attachments:
                    knowledge += "  可用附件：\n"
                    for att in attachments:
                        knowledge += f"  [附件: {att['name']} | {att['file_path']}]\n"
        
        return knowledge
    
    def get_all_documents(self) -> List[dict]:
        """获取所有文档"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, title, doc_type, category, file_path, file_url, created_at
            FROM documents
            ORDER BY created_at DESC
        """)
        
        results = []
        for row in cursor.fetchall():
            results.append({
                "id": row[0],
                "title": row[1],
                "type": row[2],
                "category": row[3],
                "file_path": row[4],
                "file_url": row[5],
                "created_at": row[6]
            })
        
        conn.close()
        return results
    
    def delete_document(self, doc_id: int) -> bool:
        """删除文档"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM keywords WHERE doc_id = ?", (doc_id,))
        cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        
        conn.commit()
        conn.close()
        return True
    
    def update_document(self, doc_id: int, title: str = None, content: str = None, 
                         category: str = None, file_path: str = None, 
                         file_url: str = None, file_type: str = None) -> bool:
        """编辑文档的标题/内容/分类/文件信息"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id, title, content, category, doc_type FROM documents WHERE id = ?", (doc_id,))
            row = cursor.fetchone()
            if not row:
                return False
            
            new_title    = title    if title    is not None else row[1]
            new_content  = content  if content  is not None else row[2]
            new_category = category if category is not None else row[3]
            new_hash     = hashlib.md5(new_content.encode()).hexdigest()
            new_doc_type = file_type if file_type is not None else row[4]
            
            # 构建更新字段
            updates = ["title=?, content=?, category=?, doc_type=?, file_hash=?, updated_at=CURRENT_TIMESTAMP"]
            params = [new_title, new_content, new_category, new_doc_type, new_hash]
            
            if file_path is not None:
                updates.append("file_path=?")
                params.append(file_path)
            if file_url is not None:
                updates.append("file_url=?")
                params.append(file_url)
            
            params.append(doc_id)
            
            sql = f"UPDATE documents SET {', '.join(updates)} WHERE id=?"
            cursor.execute(sql, params)
            
            # 重建关键词索引
            cursor.execute("DELETE FROM keywords WHERE doc_id = ?", (doc_id,))
            for kw in self._extract_keywords(new_content):
                cursor.execute("INSERT INTO keywords (doc_id, keyword) VALUES (?, ?)", (doc_id, kw))
            
            conn.commit()
            return True
        finally:
            conn.close()
    
    def get_document_by_id(self, doc_id: int) -> dict:
        """根据 ID 获取单个文档详情（含 content）"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, content, doc_type, category, file_path, file_url, created_at
            FROM documents WHERE id=?
        """, (doc_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "id": row[0], "title": row[1], "content": row[2],
            "type": row[3], "category": row[4],
            "file_path": row[5], "file_url": row[6], "created_at": row[7]
        }
    
    def get_documents(self) -> List[dict]:
        """获取所有文档（含完整信息，供前端加载）"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, doc_type, category, file_path, file_url, created_at
            FROM documents
            ORDER BY created_at DESC
        """)
        results = []
        for row in cursor.fetchall():
            results.append({
                "id": row[0],
                "title": row[1],
                "type": row[2],
                "category": row[3],
                "file_path": row[4],
                "file_url": row[5],
                "created_at": row[6]
            })
        conn.close()
        return results
    
    def get_documents_by_ids(self, doc_ids: List[int]) -> str:
        """根据文档ID列表获取完整内容，拼接成知识库字符串（包含附件信息）"""
        if not doc_ids:
            return ""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        placeholders = ','.join(['?' for _ in doc_ids])
        cursor.execute(f"""
            SELECT id, title, content FROM documents
            WHERE id IN ({placeholders})
        """, doc_ids)
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return ""
        parts = []
        for row in rows:
            doc_id = row[0]
            title = row[1]
            content = row[2]
            part = f"【{title}】\n{content}"
            
            # 添加附件信息
            attachments = self.get_attachments(doc_id)
            if attachments:
                part += "\n  可用附件："
                for att in attachments:
                    part += f"\n  [附件: {att['name']} | {att['file_path']}]"
            
            parts.append(part)
        return "\n\n".join(parts)
    
    def get_knowledge_with_attachments(self, doc_ids: List[int]) -> tuple:
        """获取知识库内容和附件信息（用于AI回复）
        
        Returns:
            (knowledge_text, attachments_list)
        """
        if not doc_ids:
            return "", []
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 获取文档内容
        placeholders = ','.join(['?' for _ in doc_ids])
        cursor.execute(f"""
            SELECT id, title, content FROM documents
            WHERE id IN ({placeholders})
        """, doc_ids)
        docs = cursor.fetchall()
        
        # 获取所有附件
        all_attachments = []
        for doc_id in doc_ids:
            cursor.execute("""
                SELECT id, name, file_name, file_path, file_url, file_type
                FROM attachments WHERE doc_id = ?
            """, (doc_id,))
            for row in cursor.fetchall():
                all_attachments.append({
                    "id": row[0],
                    "name": row[1],
                    "file_name": row[2],
                    "file_path": row[3],
                    "file_url": row[4],
                    "file_type": row[5]
                })
        
        conn.close()
        
        if not docs:
            return "", all_attachments
        
        # 构建知识文本
        parts = []
        for row in docs:
            parts.append(f"【{row[1]}】\n{row[2]}")
        knowledge_text = "\n\n".join(parts)
        
        return knowledge_text, all_attachments
    
    # ============ 附件管理方法 ============
    
    def add_attachment(self, doc_id: int, name: str, file_name: str, 
                       file_path: str, file_url: str, file_type: str = 'image') -> int:
        """为文档添加附件
        
        Args:
            name: 附件名称/描述（用于AI识别）
            file_name: 原始文件名
            
        Returns:
            附件ID
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO attachments (doc_id, name, file_name, file_path, file_url, file_type)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (doc_id, name, file_name, file_path, file_url, file_type))
        attachment_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return attachment_id
    
    def update_attachment_name(self, attachment_id: int, name: str) -> bool:
        """更新附件名称"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE attachments SET name = ? WHERE id = ?
        """, (name, attachment_id))
        updated = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return updated
    
    def get_attachments(self, doc_id: int) -> List[dict]:
        """获取文档的所有附件"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, file_name, file_path, file_url, file_type, created_at
            FROM attachments WHERE doc_id = ?
            ORDER BY created_at ASC
        """, (doc_id,))
        results = []
        for row in cursor.fetchall():
            results.append({
                "id": row[0],
                "name": row[1],
                "file_name": row[2],
                "file_path": row[3],
                "file_url": row[4],
                "file_type": row[5],
                "created_at": row[6]
            })
        conn.close()
        return results
    
    def get_all_attachments(self) -> List[dict]:
        """获取所有知识库附件（用于AI发送附件功能）"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT a.id, a.name, a.file_name, a.file_path, a.file_url, a.file_type, a.created_at, d.title as doc_title
            FROM attachments a
            JOIN documents d ON a.doc_id = d.id
            ORDER BY a.created_at ASC
        """)
        results = []
        for row in cursor.fetchall():
            results.append({
                "id": row[0],
                "name": row[1],
                "file_name": row[2],
                "file_path": row[3],
                "file_url": row[4],
                "file_type": row[5],
                "created_at": row[6],
                "doc_title": row[7]
            })
        conn.close()
        return results
    
    def get_attachment_by_id(self, attachment_id: int) -> Optional[dict]:
        """根据ID获取附件信息"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, doc_id, name, file_name, file_path, file_url, file_type
            FROM attachments WHERE id = ?
        """, (attachment_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "id": row[0], "doc_id": row[1], "name": row[2], "file_name": row[3],
            "file_path": row[4], "file_url": row[5], "file_type": row[6]
        }
    
    def delete_attachment(self, attachment_id: int) -> bool:
        """删除附件"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted
    
    def delete_attachments_by_doc(self, doc_id: int) -> int:
        """删除文档的所有附件，返回删除数量"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM attachments WHERE doc_id = ?", (doc_id,))
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count
    
    def _extract_keywords(self, text: str) -> List[str]:
        """提取关键词 - 改进版：英文单词保持完整，中文使用2-gram"""
        import re
        
        keywords = set()
        
        # 1. 提取完整英文单词（至少2个字符）
        english_words = re.findall(r'[a-zA-Z]{2,}', text)
        keywords.update(w.lower() for w in english_words)
        
        # 2. 提取数字（至少2位，避免单数字过多）
        numbers = re.findall(r'\d{2,}', text)
        keywords.update(numbers)
        
        # 3. 提取中文（2-gram）
        chinese_chars = re.findall(r'[\u4e00-\u9fff]+', text)
        for segment in chinese_chars:
            # 整个词组（如果长度适中）
            if 2 <= len(segment) <= 8:
                keywords.add(segment)
            # 2-gram
            for i in range(len(segment) - 1):
                keywords.add(segment[i:i+2])
        
        # 4. 过滤停用词
        stop_words = {'的', '了', '在', '是', '我', '有', '和', '就', '不', '人',
                     '都', '一', '一个', '上', '也', '很', '到', '说', '要', '去',
                     '你', '会', '着', '没有', '看', '好', '自己', '这', '可以',
                     '需要', '进行', '已经', '因为', '所以', '但是', '如果',
                     '我们', '他们', '这个', '那个', '什么', '怎么', '如何'}
        
        keywords = {k for k in keywords if k not in stop_words and len(k) >= 2}
        
        # 返回前15个关键词（增加数量以提高召回率）
        return list(keywords)[:15]


# 全局实例
_kb_instance: Optional[KnowledgeBase] = None


def get_knowledge_base() -> KnowledgeBase:
    """获取知识库实例"""
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = KnowledgeBase()
    return _kb_instance
