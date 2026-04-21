"""
定时发送计划服务 - 按标签筛选客户，定时逐个发送消息
"""
import os
import sqlite3
import asyncio
import json
from typing import List, Optional, Dict
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from enum import Enum
import threading


def _parse_datetime(dt_str: str) -> Optional[datetime]:
    """安全解析多种 ISO 8601 格式的时间字符串
    
    支持的格式:
    - 2024-01-15T10:30:00Z
    - 2024-01-15T10:30:00+00:00
    - 2024-01-15T10:30:00.123456+00:00
    - 2024-01-15T10:30:00
    - 2024-01-15T10:30:00.123456
    - 2024-01-15 10:30:00
    
    Args:
        dt_str: 时间字符串
        
    Returns:
        datetime 对象或 None（如果解析失败）
    """
    if not dt_str:
        return None
    
    # 处理 Z 后缀
    dt_str = dt_str.replace('Z', '+00:00')
    
    # 尝试多种格式
    for fmt in [
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%dT%H:%M:%S.%f%z',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%dT%H:%M:%S.%f',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M:%S.%f',
    ]:
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    
    # 最后尝试 fromisoformat
    try:
        return datetime.fromisoformat(dt_str)
    except ValueError:
        return None


class ScheduleStatus(Enum):
    PENDING = "pending"      # 待执行
    RUNNING = "running"      # 执行中
    COMPLETED = "completed"  # 已完成
    PAUSED = "paused"        # 已暂停
    CANCELLED = "cancelled"  # 已取消


@dataclass
class SendTask:
    """发送任务"""
    id: int
    schedule_id: int
    customer_id: int
    customer_phone: str
    customer_name: Optional[str]
    message_content: str
    status: str  # pending, sent, failed
    sent_at: Optional[str] = None
    error_msg: Optional[str] = None


@dataclass
class SendSchedule:
    """发送计划"""
    id: int
    name: str
    message_template: str
    target_tags: List[str]  # 目标标签
    target_category: Optional[str]  # 客户分类筛选
    schedule_time: str  # 计划执行时间
    interval_seconds: int  # 发送间隔（秒）
    status: str
    created_at: str
    total_count: int = 0
    sent_count: int = 0
    failed_count: int = 0


class SchedulerService:
    """定时发送服务"""
    
    def __init__(self, db_path: str = "./data/scheduler.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
        self._running_schedules: Dict[int, asyncio.Task] = {}
        self._lock = threading.Lock()
        self._running_schedules_lock = threading.Lock()  # 专门保护 _running_schedules
    
    def _init_db(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 发送计划表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS send_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                message_template TEXT NOT NULL,
                target_tags TEXT,  -- JSON数组
                target_category TEXT,
                schedule_time TIMESTAMP,
                interval_seconds INTEGER DEFAULT 60,
                status TEXT DEFAULT 'pending',
                total_count INTEGER DEFAULT 0,
                sent_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 发送任务表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS send_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER,
                customer_id INTEGER,
                customer_phone TEXT NOT NULL,
                customer_name TEXT,
                message_content TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                sent_at TIMESTAMP,
                error_msg TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (schedule_id) REFERENCES send_schedules(id)
            )
        """)
        
        conn.commit()
        conn.close()
    
    def create_schedule(self, name: str, message_template: str,
                       target_tags: List[str], target_category: Optional[str],
                       schedule_time: str, interval_seconds: int = 60) -> int:
        """
        创建发送计划
        
        Args:
            name: 计划名称
            message_template: 消息模板
            target_tags: 目标标签列表
            target_category: 客户分类筛选（new/lead/returning）
            schedule_time: 执行时间（ISO格式）
            interval_seconds: 发送间隔（秒）
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO send_schedules 
            (name, message_template, target_tags, target_category, schedule_time, interval_seconds)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            name, message_template, json.dumps(target_tags), 
            target_category, schedule_time, interval_seconds
        ))
        
        schedule_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return schedule_id
    
    def get_schedules(self, status: Optional[str] = None) -> List[SendSchedule]:
        """获取发送计划列表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if status:
            cursor.execute("""
                SELECT id, name, message_template, target_tags, target_category,
                       schedule_time, interval_seconds, status, total_count,
                       sent_count, failed_count, created_at
                FROM send_schedules WHERE status = ?
                ORDER BY created_at DESC
            """, (status,))
        else:
            cursor.execute("""
                SELECT id, name, message_template, target_tags, target_category,
                       schedule_time, interval_seconds, status, total_count,
                       sent_count, failed_count, created_at
                FROM send_schedules
                ORDER BY created_at DESC
            """)
        
        results = []
        for row in cursor.fetchall():
            results.append(SendSchedule(
                id=row[0],
                name=row[1],
                message_template=row[2],
                target_tags=json.loads(row[3]) if row[3] else [],
                target_category=row[4],
                schedule_time=row[5],
                interval_seconds=row[6],
                status=row[7],
                total_count=row[8],
                sent_count=row[9],
                failed_count=row[10],
                created_at=row[11]
            ))
        
        conn.close()
        return results
    
    def get_schedule(self, schedule_id: int) -> Optional[SendSchedule]:
        """获取计划详情"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, name, message_template, target_tags, target_category,
                   schedule_time, interval_seconds, status, total_count,
                   sent_count, failed_count, created_at
            FROM send_schedules WHERE id = ?
        """, (schedule_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return SendSchedule(
                id=row[0],
                name=row[1],
                message_template=row[2],
                target_tags=json.loads(row[3]) if row[3] else [],
                target_category=row[4],
                schedule_time=row[5],
                interval_seconds=row[6],
                status=row[7],
                total_count=row[8],
                sent_count=row[9],
                failed_count=row[10],
                created_at=row[11]
            )
        return None
    
    def get_tasks(self, schedule_id: int) -> List[SendTask]:
        """获取计划的任务列表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, schedule_id, customer_id, customer_phone, customer_name,
                   message_content, status, sent_at, error_msg
            FROM send_tasks WHERE schedule_id = ?
            ORDER BY id
        """, (schedule_id,))
        
        results = []
        for row in cursor.fetchall():
            results.append(SendTask(
                id=row[0],
                schedule_id=row[1],
                customer_id=row[2],
                customer_phone=row[3],
                customer_name=row[4],
                message_content=row[5],
                status=row[6],
                sent_at=row[7],
                error_msg=row[8]
            ))
        
        conn.close()
        return results
    
    def prepare_tasks(self, schedule_id: int, customers: List[dict]):
        """
        准备发送任务
        
        Args:
            customers: [{"id": 1, "phone": "123", "name": "张三"}]
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 获取计划信息
        schedule = self.get_schedule(schedule_id)
        if not schedule:
            conn.close()
            return
        
        # 为每个客户创建任务
        for customer in customers:
            # 个性化消息（替换变量）
            message = self._personalize_message(
                schedule.message_template, 
                customer
            )
            
            cursor.execute("""
                INSERT INTO send_tasks 
                (schedule_id, customer_id, customer_phone, customer_name, message_content)
                VALUES (?, ?, ?, ?, ?)
            """, (
                schedule_id,
                customer["id"],
                customer["phone"],
                customer.get("name"),
                message
            ))
        
        # 更新计划总数
        cursor.execute("""
            UPDATE send_schedules 
            SET total_count = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (len(customers), schedule_id))
        
        conn.commit()
        conn.close()
    
    def _personalize_message(self, template: str, customer: dict) -> str:
        """个性化消息模板"""
        message = template
        message = message.replace("{{name}}", customer.get("name") or "客户")
        message = message.replace("{{phone}}", customer.get("phone", ""))
        message = message.replace("{{category}}", customer.get("category", ""))
        return message
    
    def update_task_status(self, task_id: int, status: str, 
                          error_msg: Optional[str] = None):
        """更新任务状态"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if status == "sent":
            cursor.execute("""
                UPDATE send_tasks 
                SET status = ?, sent_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (status, task_id))
        else:
            cursor.execute("""
                UPDATE send_tasks 
                SET status = ?, error_msg = ?
                WHERE id = ?
            """, (status, error_msg, task_id))
        
        conn.commit()
        conn.close()
    
    def update_schedule_status(self, schedule_id: int, status: str):
        """更新计划状态"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE send_schedules 
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (status, schedule_id))
        
        conn.commit()
        conn.close()
    
    def update_schedule_counts(self, schedule_id: int):
        """更新计划计数"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                COUNT(CASE WHEN status = 'sent' THEN 1 END) as sent,
                COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed
            FROM send_tasks WHERE schedule_id = ?
        """, (schedule_id,))
        
        row = cursor.fetchone()
        if row:
            cursor.execute("""
                UPDATE send_schedules 
                SET sent_count = ?, failed_count = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (row[0], row[1], schedule_id))
        
        conn.commit()
        conn.close()
    
    def update_schedule(self, schedule_id: int, name: str = None, message_template: str = None,
                       target_category: str = None, target_tags: list = None, schedule_time: str = None, 
                       interval_seconds: int = None, reset_status: bool = False) -> bool:
        """更新计划内容（pending 和 completed 状态可编辑）
        
        Args:
            reset_status: 如果为 True，将 completed 状态重置为 pending
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 检查计划是否存在
        cursor.execute("SELECT status FROM send_schedules WHERE id = ?", (schedule_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        
        # 只允许编辑 pending 或 completed 状态
        current_status = row[0]
        if current_status not in [ScheduleStatus.PENDING.value, ScheduleStatus.COMPLETED.value]:
            conn.close()
            raise ValueError("只能编辑待执行或已完成的计划")
        
        # 构建更新字段
        updates = []
        params = []
        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if message_template is not None:
            updates.append("message_template = ?")
            params.append(message_template)
        if target_category is not None:
            updates.append("target_category = ?")
            params.append(target_category)
        if target_tags is not None:
            updates.append("target_tags = ?")
            params.append(json.dumps(target_tags))
        if schedule_time is not None:
            updates.append("schedule_time = ?")
            params.append(schedule_time)
        if interval_seconds is not None:
            updates.append("interval_seconds = ?")
            params.append(interval_seconds)
        
        # 如果是 completed 状态，重置为 pending 并清空计数
        if current_status == ScheduleStatus.COMPLETED.value:
            updates.append("status = ?")
            params.append(ScheduleStatus.PENDING.value)
            updates.append("sent_count = ?")
            params.append(0)
            updates.append("failed_count = ?")
            params.append(0)
        
        if not updates:
            conn.close()
            return True
        
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(schedule_id)
        
        sql = f"UPDATE send_schedules SET {', '.join(updates)} WHERE id = ?"
        cursor.execute(sql, params)
        
        conn.commit()
        conn.close()
        return True
    
    def delete_schedule(self, schedule_id: int):
        """删除计划"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 先删除任务
        cursor.execute("DELETE FROM send_tasks WHERE schedule_id = ?", (schedule_id,))
        # 再删除计划
        cursor.execute("DELETE FROM send_schedules WHERE id = ?", (schedule_id,))
        
        conn.commit()
        conn.close()
    
    def pause_schedule(self, schedule_id: int):
        """暂停计划"""
        self.update_schedule_status(schedule_id, ScheduleStatus.PAUSED.value)
        
        # 取消正在运行的任务
        with self._running_schedules_lock:
            if schedule_id in self._running_schedules:
                self._running_schedules[schedule_id].cancel()
                del self._running_schedules[schedule_id]
    
    def resume_schedule(self, schedule_id: int):
        """恢复计划"""
        self.update_schedule_status(schedule_id, ScheduleStatus.PENDING.value)
    
    def add_running_schedule(self, schedule_id: int, task: asyncio.Task):
        """添加运行中的计划任务（线程安全）"""
        with self._running_schedules_lock:
            self._running_schedules[schedule_id] = task
    
    def remove_running_schedule(self, schedule_id: int):
        """移除运行中的计划任务（线程安全）"""
        with self._running_schedules_lock:
            if schedule_id in self._running_schedules:
                del self._running_schedules[schedule_id]
    
    def get_running_schedule(self, schedule_id: int) -> Optional[asyncio.Task]:
        """获取运行中的计划任务（线程安全）"""
        with self._running_schedules_lock:
            return self._running_schedules.get(schedule_id)
    
    def is_schedule_running(self, schedule_id: int) -> bool:
        """检查计划是否正在运行（线程安全）"""
        with self._running_schedules_lock:
            return schedule_id in self._running_schedules


# 全局实例
_scheduler_service: Optional[SchedulerService] = None


def get_scheduler_service() -> SchedulerService:
    """获取定时服务实例"""
    global _scheduler_service
    if _scheduler_service is None:
        _scheduler_service = SchedulerService()
    return _scheduler_service
