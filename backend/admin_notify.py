"""
管理员通知配置数据层
- 管理员手机号列表（SQLite 持久化）
- 通知规则列表（语义化条件 + 开关）
"""
import os
import sqlite3
import json
from typing import List, Optional, Dict
from datetime import datetime


NOTIFY_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "admin_notify.db")

# ───── 内置默认规则 ─────
DEFAULT_RULES = [
    {
        "id": "first_message",
        "name": "客户首次回复",
        "description": "客户第一次向机器人发送消息时通知",
        "event_type": "first_message",
        "keywords": [],
        "enabled": True,
    },
    {
        "id": "price_inquiry",
        "name": "询价商机",
        "description": "消息中包含价格、报价、多少钱等关键词",
        "event_type": "keyword",
        "keywords": ["价格", "报价", "多少钱", "多少元", "怎么卖", "收费", "费用", "price", "cost", "quote", "how much"],
        "enabled": True,
    },
    {
        "id": "purchase_intent",
        "name": "购买意向",
        "description": "消息中包含下单、购买、要几个、需要等关键词",
        "event_type": "keyword",
        "keywords": ["下单", "购买", "要几个", "要多少", "需要", "订购", "采购", "buy", "order", "purchase"],
        "enabled": True,
    },
    {
        "id": "daily_report",
        "name": "每日报告",
        "description": "每天固定时间发送今日联系人与回复统计（默认23:59）",
        "event_type": "daily_report",
        "keywords": [],
        "enabled": True,
        "report_time": "23:59",
    },
]


class AdminNotifyDB:
    """管理员通知数据库"""

    def __init__(self, db_path: str = NOTIFY_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._conn() as conn:
            cursor = conn.cursor()
            # 管理员表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL UNIQUE,
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            # 通知规则表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS notify_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    event_type TEXT NOT NULL,
                    keywords TEXT DEFAULT '[]',
                    enabled INTEGER DEFAULT 1,
                    extra TEXT DEFAULT '{}'
                )
            """)
            conn.commit()
            # 插入默认规则（如果不存在）
            for rule in DEFAULT_RULES:
                extra = {}
                if "report_time" in rule:
                    extra["report_time"] = rule["report_time"]
                cursor.execute("""
                    INSERT OR IGNORE INTO notify_rules (id, name, description, event_type, keywords, enabled, extra)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    rule["id"], rule["name"], rule["description"],
                    rule["event_type"], json.dumps(rule["keywords"], ensure_ascii=False),
                    1 if rule["enabled"] else 0,
                    json.dumps(extra, ensure_ascii=False)
                ))
            conn.commit()

    # ───── 管理员 CRUD ─────

    def add_admin(self, name: str, phone: str) -> int:
        """添加管理员，返回 id"""
        # 清理手机号
        clean = ''.join(c for c in phone if c.isdigit() or c == '+')
        if clean.startswith('+'):
            clean = clean[1:]
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO admins (name, phone, enabled) VALUES (?, ?, 1)",
                (name, clean)
            )
            conn.commit()
            return cursor.lastrowid

    def get_admins(self) -> List[Dict]:
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, phone, enabled, created_at FROM admins ORDER BY id")
            rows = cursor.fetchall()
        return [{"id": r[0], "name": r[1], "phone": r[2], "enabled": bool(r[3]), "created_at": r[4]} for r in rows]

    def get_active_admin_phones(self) -> List[str]:
        """获取所有启用的管理员手机号"""
        admins = self.get_admins()
        return [a["phone"] for a in admins if a["enabled"]]

    def update_admin(self, admin_id: int, name: str = None, phone: str = None, enabled: bool = None) -> bool:
        with self._conn() as conn:
            cursor = conn.cursor()
            if name is not None:
                cursor.execute("UPDATE admins SET name=? WHERE id=?", (name, admin_id))
            if phone is not None:
                clean = ''.join(c for c in phone if c.isdigit() or c == '+')
                if clean.startswith('+'):
                    clean = clean[1:]
                cursor.execute("UPDATE admins SET phone=? WHERE id=?", (clean, admin_id))
            if enabled is not None:
                cursor.execute("UPDATE admins SET enabled=? WHERE id=?", (1 if enabled else 0, admin_id))
            conn.commit()
        return True

    def delete_admin(self, admin_id: int) -> bool:
        with self._conn() as conn:
            conn.execute("DELETE FROM admins WHERE id=?", (admin_id,))
            conn.commit()
        return True

    # ───── 通知规则 CRUD ─────

    def get_rules(self) -> List[Dict]:
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, description, event_type, keywords, enabled, extra FROM notify_rules ORDER BY rowid")
            rows = cursor.fetchall()
        result = []
        for r in rows:
            extra = json.loads(r[6]) if r[6] else {}
            rule = {
                "id": r[0], "name": r[1], "description": r[2],
                "event_type": r[3],
                "keywords": json.loads(r[4]) if r[4] else [],
                "enabled": bool(r[5]),
            }
            rule.update(extra)
            result.append(rule)
        return result

    def get_rule(self, rule_id: str) -> Optional[Dict]:
        rules = self.get_rules()
        return next((r for r in rules if r["id"] == rule_id), None)

    def update_rule(self, rule_id: str, enabled: bool = None, keywords: list = None,
                    name: str = None, description: str = None, report_time: str = None) -> bool:
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT extra FROM notify_rules WHERE id=?", (rule_id,))
            row = cursor.fetchone()
            if not row:
                return False
            extra = json.loads(row[0]) if row[0] else {}
            if report_time is not None:
                extra["report_time"] = report_time
            if enabled is not None:
                cursor.execute("UPDATE notify_rules SET enabled=? WHERE id=?", (1 if enabled else 0, rule_id))
            if keywords is not None:
                cursor.execute("UPDATE notify_rules SET keywords=? WHERE id=?",
                               (json.dumps(keywords, ensure_ascii=False), rule_id))
            if name is not None:
                cursor.execute("UPDATE notify_rules SET name=? WHERE id=?", (name, rule_id))
            if description is not None:
                cursor.execute("UPDATE notify_rules SET description=? WHERE id=?", (description, rule_id))
            cursor.execute("UPDATE notify_rules SET extra=? WHERE id=?",
                           (json.dumps(extra, ensure_ascii=False), rule_id))
            conn.commit()
        return True

    def add_custom_rule(self, rule_id: str, name: str, description: str, keywords: List[str]) -> bool:
        """添加自定义关键词规则"""
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO notify_rules (id, name, description, event_type, keywords, enabled, extra)
                VALUES (?, ?, ?, 'keyword', ?, 1, '{}')
            """, (rule_id, name, description, json.dumps(keywords, ensure_ascii=False)))
            conn.commit()
        return True

    def delete_custom_rule(self, rule_id: str) -> bool:
        """只允许删除自定义规则（内置规则不能删除）"""
        builtin_ids = {r["id"] for r in DEFAULT_RULES}
        if rule_id in builtin_ids:
            return False
        with self._conn() as conn:
            conn.execute("DELETE FROM notify_rules WHERE id=?", (rule_id,))
            conn.commit()
        return True


# ───── 全局单例 ─────
_admin_notify_db: Optional[AdminNotifyDB] = None


def get_admin_notify_db() -> AdminNotifyDB:
    global _admin_notify_db
    if _admin_notify_db is None:
        _admin_notify_db = AdminNotifyDB()
    return _admin_notify_db
