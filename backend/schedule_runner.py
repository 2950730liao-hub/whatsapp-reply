"""
定时计划执行器 - 后台执行发送任务
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from database import get_db, Customer
from scheduler_service import get_scheduler_service, ScheduleStatus, _parse_datetime
from whatsapp_client import WhatsAppClient


class ScheduleRunner:
    """计划执行器"""
    
    def __init__(self, whatsapp_client: Optional[WhatsAppClient] = None):
        self.scheduler = get_scheduler_service()
        self.whatsapp_client = whatsapp_client
        self._running = False
        self._task: Optional[asyncio.Task] = None
    
    def start(self):
        """启动执行器"""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._run_loop())
            print("[ScheduleRunner] 定时发送执行器已启动")
    
    def stop(self):
        """停止执行器"""
        self._running = False
        if self._task:
            self._task.cancel()
            print("[ScheduleRunner] 定时发送执行器已停止")
    
    async def _run_loop(self):
        """主循环"""
        while self._running:
            try:
                await self._check_and_execute()
                await asyncio.sleep(10)  # 每10秒检查一次
            except Exception as e:
                print(f"[ScheduleRunner] 执行错误: {e}")
                await asyncio.sleep(30)
    
    async def _check_and_execute(self):
        """检查并执行到期的计划"""
        # 获取待执行的计划
        pending_schedules = self.scheduler.get_schedules(ScheduleStatus.PENDING.value)
        
        for schedule in pending_schedules:
            # 检查是否到达执行时间
            try:
                schedule_time = _parse_datetime(schedule.schedule_time)
                if schedule_time and datetime.now().replace(tzinfo=None) >= schedule_time.replace(tzinfo=None):
                    # 开始执行
                    asyncio.create_task(self._execute_schedule(schedule.id))
            except Exception as e:
                print(f"[ScheduleRunner] 时间解析错误: {e}")
    
    async def _execute_schedule(self, schedule_id: int):
        """执行发送计划"""
        schedule = self.scheduler.get_schedule(schedule_id)
        if not schedule or schedule.status != ScheduleStatus.PENDING.value:
            return
        
        print(f"[ScheduleRunner] 开始执行计划: {schedule.name}")
        
        # 更新状态为执行中
        self.scheduler.update_schedule_status(schedule_id, ScheduleStatus.RUNNING.value)
        
        # 获取任务列表
        tasks = self.scheduler.get_tasks(schedule_id)
        
        if not tasks:
            print(f"[ScheduleRunner] 计划没有任务: {schedule.name}")
            self.scheduler.update_schedule_status(schedule_id, ScheduleStatus.COMPLETED.value)
            return
        
        # 逐个发送
        for task in tasks:
            # 检查是否已暂停或取消
            current = self.scheduler.get_schedule(schedule_id)
            if not current or current.status in [ScheduleStatus.PAUSED.value, ScheduleStatus.CANCELLED.value]:
                print(f"[ScheduleRunner] 计划已暂停/取消: {schedule.name}")
                return
            
            # 跳过已发送的任务
            if task.status == "sent":
                continue
            
            # 发送消息
            success = await self._send_message(task)
            
            if success:
                self.scheduler.update_task_status(task.id, "sent")
                print(f"[ScheduleRunner] 发送成功: {task.customer_phone}")
            else:
                self.scheduler.update_task_status(task.id, "failed", "发送失败")
                print(f"[ScheduleRunner] 发送失败: {task.customer_phone}")
            
            # 更新计数
            self.scheduler.update_schedule_counts(schedule_id)
            
            # 等待间隔
            if task != tasks[-1]:  # 不是最后一个
                await asyncio.sleep(schedule.interval_seconds)
        
        # 标记完成
        self.scheduler.update_schedule_status(schedule_id, ScheduleStatus.COMPLETED.value)
        print(f"[ScheduleRunner] 计划执行完成: {schedule.name}")
    
    async def _send_message(self, task) -> bool:
        """发送消息"""
        if not self.whatsapp_client:
            print("[ScheduleRunner] WhatsApp 客户端未就绪")
            return False
        
        try:
            return self.whatsapp_client.send_message(task.customer_phone, task.message_content)
        except Exception as e:
            print(f"[ScheduleRunner] 发送异常: {e}")
            return False
    
    def execute_now(self, schedule_id: int):
        """立即执行计划"""
        asyncio.create_task(self._execute_schedule(schedule_id))


# 全局实例
_schedule_runner: Optional[ScheduleRunner] = None


def get_schedule_runner(whatsapp_client: Optional[WhatsAppClient] = None) -> ScheduleRunner:
    """获取计划执行器"""
    global _schedule_runner
    if _schedule_runner is None:
        _schedule_runner = ScheduleRunner(whatsapp_client)
    elif whatsapp_client:
        _schedule_runner.whatsapp_client = whatsapp_client
    return _schedule_runner
