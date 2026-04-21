"""
通知服务
- 商机事件检测（关键词匹配 / 首次回复）
- 向所有启用的管理员发送 WhatsApp 通知
- 每日报告生成与发送
"""
import logging
from datetime import datetime, date
from typing import Optional, List

from admin_notify import get_admin_notify_db

logger = logging.getLogger(__name__)


class NotifyService:
    """通知服务"""

    def __init__(self, whatsapp_client=None):
        self.wa_client = whatsapp_client

    def set_client(self, whatsapp_client):
        self.wa_client = whatsapp_client

    # ─────────────────────────────────────────────
    #  核心：检查消息是否触发通知，并发出
    # ─────────────────────────────────────────────

    def check_and_notify(self, customer_phone: str, customer_name: str,
                         message_content: str, is_first_message: bool = False):
        """
        在收到客户消息时调用。
        根据规则判断是否需要通知管理员。
        """
        if not self.wa_client:
            return

        db = get_admin_notify_db()
        admin_phones = db.get_active_admin_phones()
        if not admin_phones:
            return

        rules = db.get_rules()
        triggered = []

        for rule in rules:
            if not rule.get("enabled"):
                continue

            event_type = rule.get("event_type")

            if event_type == "first_message" and is_first_message:
                triggered.append(rule)

            elif event_type == "keyword":
                keywords = rule.get("keywords", [])
                if self._match_keywords(message_content, keywords):
                    triggered.append(rule)

        for rule in triggered:
            notice = self._build_notice(rule, customer_phone, customer_name, message_content)
            self._send_to_admins(admin_phones, notice)

    def _match_keywords(self, text: str, keywords: List[str]) -> bool:
        """关键词匹配（不区分大小写）"""
        lower = text.lower()
        return any(kw.lower() in lower for kw in keywords if kw)

    def _build_notice(self, rule: dict, phone: str, name: str, content: str) -> str:
        event_type = rule.get("event_type")
        display_name = name if name and name != phone else f"+{phone}"
        short_content = content[:60] + ("..." if len(content) > 60 else "")

        if event_type == "first_message":
            return (
                f"📩 *首次消息提醒*\n"
                f"客户：{display_name}（{phone}）\n"
                f"内容：{short_content}"
            )
        elif event_type == "keyword":
            rule_name = rule.get("name", "商机")
            return (
                f"🔥 *{rule_name}*\n"
                f"客户：{display_name}（{phone}）\n"
                f"内容：{short_content}"
            )
        else:
            return f"🔔 事件通知\n客户：{phone}\n内容：{short_content}"

    def _send_to_admins(self, admin_phones: List[str], text: str):
        """发送消息给所有管理员"""
        for phone in admin_phones:
            try:
                self.wa_client.send_message(phone, text)
                logger.info(f"[NotifyService] 已通知管理员 {phone}")
            except Exception as e:
                logger.error(f"[NotifyService] 通知管理员 {phone} 失败: {e}")

    # ─────────────────────────────────────────────
    #  每日报告
    # ─────────────────────────────────────────────

    def send_daily_report(self):
        """生成并发送每日报告（供定时任务调用）"""
        if not self.wa_client:
            logger.warning("[NotifyService] 日报：WhatsApp 客户端未就绪")
            return

        db = get_admin_notify_db()

        # 检查日报规则是否启用
        rule = db.get_rule("daily_report")
        if not rule or not rule.get("enabled"):
            return

        admin_phones = db.get_active_admin_phones()
        if not admin_phones:
            return

        # 统计今日数据
        report_text = self._generate_daily_report()
        self._send_to_admins(admin_phones, report_text)

    def _generate_daily_report(self) -> str:
        """生成今日报告文本"""
        try:
            from database import SessionLocal, Customer, Message
            from sqlalchemy import func
            today = date.today()
            today_start = datetime(today.year, today.month, today.day, 0, 0, 0)
            today_end = datetime(today.year, today.month, today.day, 23, 59, 59)

            db = SessionLocal()
            try:
                # 今日收到消息的客户数（去重）
                contacted = db.query(func.count(func.distinct(Message.customer_id))).filter(
                    Message.direction == "incoming",
                    Message.created_at >= today_start,
                    Message.created_at <= today_end
                ).scalar() or 0

                # 今日有收到客户消息的客户 ID 集合
                customer_ids_today = db.query(Message.customer_id).filter(
                    Message.direction == "incoming",
                    Message.created_at >= today_start,
                    Message.created_at <= today_end
                ).distinct().all()
                customer_ids_today = [r[0] for r in customer_ids_today]

                # 有回复的客户数（今日既发出消息又收到消息）
                replied = 0
                if customer_ids_today:
                    replied = db.query(func.count(func.distinct(Message.customer_id))).filter(
                        Message.direction == "outgoing",
                        Message.customer_id.in_(customer_ids_today),
                        Message.created_at >= today_start,
                        Message.created_at <= today_end
                    ).scalar() or 0

                # 新增客户数
                new_customers = db.query(func.count(Customer.id)).filter(
                    Customer.created_at >= today_start,
                    Customer.created_at <= today_end
                ).scalar() or 0

            finally:
                db.close()

            today_str = today.strftime("%m月%d日")
            return (
                f"📊 *每日报告 · {today_str}*\n\n"
                f"• 今日联系客户：{contacted} 人\n"
                f"• 已获得回复：{replied} 人\n"
                f"• 新增客户：{new_customers} 人\n\n"
                f"_由 WhatsApp 机器人自动发送_"
            )

        except Exception as e:
            logger.error(f"[NotifyService] 生成日报失败: {e}")
            today_str = date.today().strftime("%m月%d日")
            return f"📊 每日报告 · {today_str}\n（数据统计失败，请检查服务状态）"


# ─────────────────────────────────────────────
#  全局单例
# ─────────────────────────────────────────────
_notify_service: Optional[NotifyService] = None


def get_notify_service() -> NotifyService:
    global _notify_service
    if _notify_service is None:
        _notify_service = NotifyService()
    return _notify_service
