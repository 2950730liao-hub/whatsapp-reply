"""
Neonize WhatsApp 客户端封装
替代原有的 CLI 方案，提供更稳定的连接
"""
import os
import json
import asyncio
import threading
import concurrent.futures
import time
import logging
import queue
from collections import OrderedDict
from typing import Optional, List, Dict, Callable
from datetime import datetime, timedelta
from database import SessionLocal, Customer, Message, Conversation
from whatsapp_interface import IWhatsAppClient

logger = logging.getLogger(__name__)

# 延迟导入 neonize，避免启动时加载
try:
    from neonize.client import NewClient
    from neonize.events import (
        MessageEv, ConnectedEv, DisconnectedEv, QREv,
        ConnectFailureEv, LoggedOutEv, event,
        HistorySyncEv, ReceiptEv, ChatPresenceEv, PresenceEv,
        KeepAliveTimeoutEv, KeepAliveRestoredEv,
        StreamReplacedEv, StreamErrorEv,
        OfflineSyncPreviewEv, OfflineSyncCompletedEv,
        PairStatusEv
    )
    NEONIZE_AVAILABLE = True
except ImportError:
    NEONIZE_AVAILABLE = False
    print("[Neonize] 未安装，请运行: pip install neonize")
    # 定义占位符，避免类定义时 NameError
    class NewClient: pass
    class MessageEv: pass
    class ConnectedEv: pass
    class DisconnectedEv: pass
    class QREv: pass
    class ConnectFailureEv: pass
    class LoggedOutEv: pass
    class HistorySyncEv: pass
    class ReceiptEv: pass
    class ChatPresenceEv: pass
    class PresenceEv: pass
    class KeepAliveTimeoutEv: pass
    class KeepAliveRestoredEv: pass
    class StreamReplacedEv: pass
    class StreamErrorEv: pass
    class OfflineSyncPreviewEv: pass
    class OfflineSyncCompletedEv: pass
    class PairStatusEv: pass
    class event:
        @staticmethod
        def handler(*args, **kwargs):
            def decorator(f): return f
            return decorator


# 全局二维码存储（用于前端获取）
_current_qr_code: Optional[str] = None
_qr_lock = threading.Lock()

# 客户端就绪事件（用于同步）
_client_ready_event: Optional[threading.Event] = None

def set_client_ready_event(event: threading.Event):
    """设置客户端就绪事件"""
    global _client_ready_event
    _client_ready_event = event

def get_current_qr() -> Optional[str]:
    """获取当前二维码（供前端调用）"""
    with _qr_lock:
        return _current_qr_code

def clear_current_qr():
    """清除当前二维码"""
    global _current_qr_code
    with _qr_lock:
        _current_qr_code = None


class NeonizeWhatsAppClient(IWhatsAppClient):
    """基于 Neonize 的 WhatsApp 客户端"""
    
    def __init__(self, name: str = "whatsapp_crm"):
        self.name = name
        self.client: Optional[NewClient] = None
        self.connected = False
        self.message_handlers: List[Callable] = []
        self._handlers_lock = threading.Lock()
        
        # 消息去重机制 - 使用 OrderedDict 存储 ID 和时间戳
        self._received_message_ids = OrderedDict()
        self._msg_id_lock = threading.Lock()
        self._max_cached_ids = 10000  # 最大缓存数
        self._message_dedup_ttl = timedelta(minutes=30)  # 消息去重保留时间
        
        # 消息队列机制
        self._message_queue = queue.Queue(maxsize=5000)
        self._queue_worker = None
        self._queue_running = False
        self._queue_high_watermark = 4000  # 80% 告警阈值
        self._queue_max_retries = 3  # 队列满时最大重试次数
        self._queue_retry_delay = 0.5  # 重试等待时间（秒）
        
        # 心跳检测机制
        self._last_message_time = time.time()
        self._consecutive_send_failures = 0
        self._heartbeat_lock = threading.Lock()
        self._heartbeat_inactive_threshold = timedelta(minutes=5)  # 5分钟无消息告警
        
        # 共享线程池（问题1修复）
        self._send_executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        
        # LID缓存（问题3修复）
        self._lid_cache = {}
        
        if not NEONIZE_AVAILABLE:
            raise RuntimeError("Neonize 未安装")
    
    def on_message(self, handler: Callable):
        """注册消息处理器"""
        with self._handlers_lock:
            self.message_handlers.append(handler)
    
    def _cleanup_old_message_ids(self):
        """清理超过30分钟的旧消息ID"""
        current_time = time.time()
        with self._msg_id_lock:
            # 找出过期的消息ID
            expired_ids = [
                msg_id for msg_id, timestamp in self._received_message_ids.items()
                if current_time - timestamp > self._message_dedup_ttl.total_seconds()
            ]
            # 删除过期条目
            for msg_id in expired_ids:
                del self._received_message_ids[msg_id]
            if expired_ids:
                logger.debug(f"[Neonize] 清理 {len(expired_ids)} 条过期消息ID")

    def _handle_incoming_message(self, client: NewClient, event: MessageEv):
        """处理收到的消息 - 快速入队模式"""
        current_time = time.time()
        
        # 清理过期消息ID（只在超过阈值的80%时才触发清理，问题4修复）
        if len(self._received_message_ids) > self._max_cached_ids * 0.8:
            self._cleanup_old_message_ids()
        
        # 消息去重检查
        msg_id = str(event.Info.ID) if event.Info.ID else None
        if msg_id:
            with self._msg_id_lock:
                if msg_id in self._received_message_ids:
                    logger.debug(f"[Neonize] 跳过重复消息: {msg_id}")
                    return
                # 存储消息ID和时间戳
                self._received_message_ids[msg_id] = current_time
                # 防止内存泄漏：超过上限清理最旧的 ID
                if len(self._received_message_ids) > self._max_cached_ids:
                    # 删除最旧的条目（OrderedDict 保持插入顺序）
                    while len(self._received_message_ids) > self._max_cached_ids // 2:
                        self._received_message_ids.popitem(last=False)
                    logger.debug(f"[Neonize] 消息ID缓存超过上限，清理至 {len(self._received_message_ids)} 条")
        
        # 更新最后消息时间（用于心跳检测）
        with self._heartbeat_lock:
            self._last_message_time = current_time
        
        # 检查队列利用率并告警
        queue_size = self._message_queue.qsize()
        if queue_size >= self._queue_high_watermark:
            logger.warning(f"[Neonize] 消息队列利用率超过80%: {queue_size}/5000")
        
        # 入队（带有限重试）
        for attempt in range(self._queue_max_retries):
            try:
                self._message_queue.put_nowait(event)
                logger.debug(f"[Neonize] 消息已入队，当前队列大小: {queue_size}")
                return
            except queue.Full:
                if attempt < self._queue_max_retries - 1:
                    logger.warning(f"[Neonize] 消息队列已满，第 {attempt + 1} 次重试...")
                    time.sleep(self._queue_retry_delay)
                else:
                    # 队列满且重试失败，改为同步处理而非丢弃（问题2修复）
                    logger.warning(f"[Neonize] 队列持续满，改为同步处理消息...")
                    try:
                        self._process_single_message(event)
                    except Exception as e:
                        logger.error(f"[Neonize] 同步处理消息失败: {e}", exc_info=True)
                    return
    
    def _process_message_queue(self):
        """后台线程：处理消息队列"""
        while self._queue_running:
            try:
                event = self._message_queue.get(timeout=1)
                self._process_single_message(event)
                self._message_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"[Neonize] 队列处理异常: {e}", exc_info=True)
    
    def _process_single_message(self, event: MessageEv):
        """处理单条消息 - 包含数据库操作"""
        print(f"[Neonize] 🔔 消息事件被触发！")
        db = SessionLocal()
        try:
            # 获取消息内容 - 处理不同类型的消息
            message_text = ""
            if event.Message.conversation:
                message_text = event.Message.conversation
            elif event.Message.extendedTextMessage and event.Message.extendedTextMessage.text:
                message_text = event.Message.extendedTextMessage.text
            
            # 获取消息源信息（字段名大写开头）
            sender_jid = event.Info.MessageSource.Sender
            chat_jid = event.Info.MessageSource.Chat
            is_from_me = event.Info.MessageSource.IsFromMe
            
            # 提取手机号（JID格式：123456789@s.whatsapp.net 或 123456789@lid）
            # sender_jid 可能是 JID 对象或字符串
            if hasattr(sender_jid, 'User'):
                # 是 JID 对象
                sender_phone = sender_jid.User
                sender_server = sender_jid.Server if hasattr(sender_jid, 'Server') else ""
            elif isinstance(sender_jid, str):
                # 是字符串
                sender_phone = sender_jid.split("@")[0] if sender_jid else ""
                sender_server = sender_jid.split("@")[1] if "@" in sender_jid else ""
            else:
                sender_phone = str(sender_jid) if sender_jid else ""
                sender_server = ""
            
            # 如果是 LID 格式，尝试转换为手机号
            if sender_server == "lid" or "@lid" in str(sender_jid):
                original_id = sender_phone
                sender_phone = self._lid_to_phone(sender_phone)
                if sender_phone != original_id:
                    print(f"[Neonize] 🔄 LID 转换: {original_id} -> {sender_phone}")
            
            print(f"[Neonize] 📩 收到消息 from {sender_phone}: {message_text[:50]}...")
            print(f"[Neonize]    是否自己发送: {is_from_me}")
            
            # 获取或创建客户
            customer = db.query(Customer).filter(
                Customer.phone == sender_phone
            ).first()
            
            if not customer:
                customer = Customer(
                    phone=sender_phone,
                    name=sender_phone,
                    category="new"
                )
                db.add(customer)
                db.flush()  # 获取 ID，但不提交
                print(f"[Neonize] 创建新客户: {sender_phone}")
            
            # 获取或创建会话
            conversation = db.query(Conversation).filter(
                Conversation.customer_id == customer.id
            ).first()
            
            if not conversation:
                conversation = Conversation(
                    customer_id=customer.id,
                    status="bot"
                )
                db.add(conversation)
                db.flush()  # 获取 ID，但不提交
            
            # 创建消息记录
            from database import Message as DBMessage
            msg = DBMessage(
                customer_id=customer.id,
                wa_message_id=str(event.Info.ID) if event.Info.ID else "",
                content=message_text,
                direction="outgoing" if is_from_me else "incoming",
                sender_name=sender_phone if not is_from_me else "AI助手",
                message_type="text"
            )
            db.add(msg)
            
            # 单次提交所有更改
            db.commit()
            
            # 刷新对象以获取生成的 ID
            db.refresh(customer)
            db.refresh(msg)
            
            # 自动打标签（新消息到达时触发）
            if not is_from_me:
                try:
                    self._apply_auto_tags(db, customer.id, message_text)
                except Exception as e:
                    logger.warning(f"[Neonize] 自动打标签失败: {e}")
            
            # 触发消息处理器（使用副本避免竞态条件）
            with self._handlers_lock:
                handlers_copy = self.message_handlers[:]
            for handler in handlers_copy:
                try:
                    handler(msg, customer)
                except Exception as e:
                    logger.error(f"[Neonize] 处理器错误: {e}", exc_info=True)
            
        except Exception as e:
            db.rollback()
            logger.error(f"[Neonize] 消息处理失败: {e}", exc_info=True)
            import traceback
            traceback.print_exc()
        finally:
            db.close()
    
    def _apply_auto_tags(self, db, customer_id: int, message_content: str = ""):
        """根据自动标签规则为客户打标签
        
        规则逻辑：
        - 新客户：对方没有回复，或简单信息往来（传入消息数<=2且来回互动少）
        - 跟进中：有来回对答，或问了价格/需求
        - 老客户：有报价有交易关键词
        """
        try:
            from database import AutoTagRule, CustomerTagAssociation, CustomerTagLog, Message as DBMessage
        except ImportError:
            return
        
        # 统计消息数据（一次性查询，避免重复查询）
        total_msg_count = db.query(DBMessage).filter(DBMessage.customer_id == customer_id).count()
        incoming_count = db.query(DBMessage).filter(
            DBMessage.customer_id == customer_id,
            DBMessage.direction == "incoming"
        ).count()
        outgoing_count = db.query(DBMessage).filter(
            DBMessage.customer_id == customer_id,
            DBMessage.direction == "outgoing"
        ).count()
        
        # 是否有双向互动（有来有往）
        has_two_way_chat = incoming_count >= 2 and outgoing_count >= 1
        
        # 价格/需求关键词
        price_keywords = [
            "报价", "价格", "多少錢", "多少錢", "费用", "价位",
            "需求", "定制", "投标", "采购",
            "cost", "price", "quote", "pricing", "budget", "how much", "rate"
        ]
        # 交易关键词
        deal_keywords = [
            "成交", "订单", "付款", "购买", "购入", "合同", "发货",
            "起单", "订货", "签订", "定金", "考虑合作",
            "order", "purchase", "deal", "confirmed", "payment", "invoice", "contract"
        ]
        
        content_lower = message_content.lower() if message_content else ""
        has_price_inquiry = any(kw in content_lower for kw in price_keywords)
        has_deal_intent = any(kw in content_lower for kw in deal_keywords)
        
        rules = db.query(AutoTagRule).filter(AutoTagRule.is_active == True).order_by(AutoTagRule.priority.desc()).all()
        applied = []
        removed = []
        
        for rule in rules:
            should_apply = False
            
            if rule.condition_type == "new_customer":
                # 新客户：没有回复，或只有少量展示性消息（对方信息<=2且无双向互动）
                should_apply = incoming_count <= 2 and not has_two_way_chat and not has_price_inquiry
            
            elif rule.condition_type == "follow_up" or rule.condition_type == "message_received":
                # 跟进中：有来回对答，或问了价格/需求
                should_apply = has_two_way_chat or has_price_inquiry
            
            elif rule.condition_type == "old_customer":
                # 老客户：有报价有交易意向
                should_apply = (has_price_inquiry or has_two_way_chat) and has_deal_intent
            
            elif rule.condition_type == "quote_requested":
                # 价格和需求关键词匹配
                should_apply = has_price_inquiry
            
            elif rule.condition_type == "keyword_match" and message_content:
                keywords = rule.condition_config.get("keywords", []) if rule.condition_config else []
                should_apply = any(kw in content_lower for kw in keywords)
            
            if should_apply:
                existing = db.query(CustomerTagAssociation).filter(
                    CustomerTagAssociation.customer_id == customer_id,
                    CustomerTagAssociation.tag_id == rule.tag_id
                ).first()
                if not existing:
                    db.add(CustomerTagAssociation(customer_id=customer_id, tag_id=rule.tag_id))
                    db.add(CustomerTagLog(customer_id=customer_id, tag_id=rule.tag_id, action="add", source="auto_rule", source_id=rule.id))
                    applied.append(rule.tag_id)
        
        if applied:
            db.commit()
            print(f"[Neonize] 自动打标签: 客户 {customer_id} 新增 {len(applied)} 个标签 (incoming={incoming_count}, outgoing={outgoing_count}, price={has_price_inquiry}, deal={has_deal_intent})")
    
    def _on_connected(self, client: NewClient, event: ConnectedEv):
        """连接成功回调"""
        self.connected = True
        clear_current_qr()  # 清除二维码
        # 通知客户端已就绪
        if _client_ready_event:
            _client_ready_event.set()
        print(f"[Neonize] ✅ 已连接到 WhatsApp")
        print(f"[Neonize] 设备: {event.device if hasattr(event, 'device') else 'Unknown'}")
    
    def _on_disconnected(self, client: NewClient, event: DisconnectedEv):
        """断开连接回调"""
        self.connected = False
        print(f"[Neonize] ⚠️ 与 WhatsApp 断开连接")
        if hasattr(event, 'reason'):
            print(f"[Neonize] 断开原因: {event.reason}")
    
    def _on_connect_failure(self, client: NewClient, event: ConnectFailureEv):
        """连接失败回调"""
        self.connected = False
        print(f"[Neonize] ❌ 连接失败")
        if hasattr(event, 'reason'):
            print(f"[Neonize] 失败原因: {event.reason}")
        if hasattr(event, 'error'):
            print(f"[Neonize] 错误信息: {event.error}")
    
    def _on_logged_out(self, client: NewClient, event: LoggedOutEv):
        """登出回调"""
        self.connected = False
        print(f"[Neonize] 👋 已登出")
        if hasattr(event, 'reason'):
            print(f"[Neonize] 登出原因: {event.reason}")
    
    def _on_qr_code(self, client: NewClient, event: QREv):
        """二维码回调 - 异步版本（未使用）"""
        pass
    
    def _on_qr_code_sync(self, client: NewClient, qr_data: bytes):
        """二维码回调 - 同步版本（实际使用）"""
        global _current_qr_code
        try:
            print(f"[Neonize] 📱 收到二维码数据")
            print(f"[Neonize] 数据类型: {type(qr_data)}")
            
            if qr_data:
                # 将 bytes 转换为字符串
                with _qr_lock:
                    if isinstance(qr_data, bytes):
                        _current_qr_code = qr_data.decode('utf-8')
                    else:
                        _current_qr_code = str(qr_data)
                    qr_len = len(_current_qr_code)
                    qr_preview = _current_qr_code[:50]
                print(f"[Neonize] ✅ 二维码已保存，长度: {qr_len}")
                print(f"[Neonize] 二维码预览: {qr_preview}...")
        except Exception as e:
            print(f"[Neonize] ❌ 处理二维码错误: {e}")
            import traceback
            traceback.print_exc()
    
    def connect(self):
        """连接到 WhatsApp"""
        print(f"[Neonize] 正在初始化客户端...")
        
        self.client = NewClient(self.name)
        
        # 注册事件处理器（必须注册所有可能被 Go 触发的事件，否则 KeyError 会中断事件循环）
        self.client.event(ConnectedEv)(self._on_connected)
        self.client.event(DisconnectedEv)(self._on_disconnected)
        self.client.event(MessageEv)(self._handle_incoming_message)
        self.client.event(ConnectFailureEv)(self._on_connect_failure)
        self.client.event(LoggedOutEv)(self._on_logged_out)
        # 注册其余事件（空处理，防止 KeyError 中断事件循环）
        _noop = lambda c, e: None
        self.client.event(HistorySyncEv)(_noop)
        self.client.event(ReceiptEv)(_noop)
        self.client.event(ChatPresenceEv)(_noop)
        self.client.event(PresenceEv)(_noop)
        self.client.event(KeepAliveTimeoutEv)(_noop)
        self.client.event(KeepAliveRestoredEv)(_noop)
        self.client.event(StreamReplacedEv)(_noop)
        self.client.event(StreamErrorEv)(_noop)
        self.client.event(OfflineSyncPreviewEv)(_noop)
        self.client.event(OfflineSyncCompletedEv)(_noop)
        self.client.event(PairStatusEv)(_noop)
        # 使用 qr 方法注册二维码回调（同步客户端）
        self.client.qr(self._on_qr_code_sync)
        
        # 打印注册的事件处理器
        print(f"[Neonize] 已注册事件处理器: {list(self.client.event.list_func.keys())}")
        
        # 启动消息队列工作线程（确保只启动一次）
        if not self._queue_running:
            self._queue_running = True
            self._queue_worker = threading.Thread(target=self._process_message_queue, daemon=True)
            self._queue_worker.start()
            print(f"[Neonize] 消息队列处理线程已启动")
        else:
            print(f"[Neonize] 消息队列处理线程已在运行，跳过启动")
        
        # 启动连接（非阻塞）
        def run_client():
            try:
                print(f"[Neonize] 正在连接...")
                self.client.connect()
                print(f"[Neonize] 连接已建立，保持运行...")
                # 通知初始化完成
                if _client_ready_event:
                    _client_ready_event.set()
                # 使用循环代替 event.wait()，避免阻塞事件处理
                last_check = time.time()
                while True:
                    current_time = time.time()
                    if current_time - last_check > 30:  # 每30秒检查一次
                        if not self.connected:
                            logger.warning("[Neonize] 检测到连接断开，退出循环准备重连")
                            break
                        
                        # 心跳检测逻辑
                        with self._heartbeat_lock:
                            time_since_last_msg = current_time - self._last_message_time
                            failures = self._consecutive_send_failures
                        
                        # 检查连续发送失败次数
                        if failures >= 3:
                            logger.error(f"[Neonize] 连续发送失败 {failures} 次，连接可能已失效，触发重连")
                            self.connected = False
                            break
                        
                        # 检查消息活跃度（超过5分钟无消息且系统应该活跃）
                        if time_since_last_msg > self._heartbeat_inactive_threshold.total_seconds():
                            logger.warning(f"[Neonize] 超过阈值时间未处理消息，连接可能僵死，触发重连")
                            self.connected = False
                            break  # 退出循环触发重连（问题5修复）
                        
                        logger.debug("[Neonize] 连接状态正常")
                        last_check = current_time
                    time.sleep(1)
            except Exception as e:
                print(f"[Neonize] 客户端错误: {e}")
                import traceback
                traceback.print_exc()
            finally:
                self.connected = False
        
        thread = threading.Thread(target=run_client, daemon=True)
        thread.start()
        
        print(f"[Neonize] 客户端已在后台启动")
        return thread
    
    def get_chat_jid(self, phone: str) -> Optional[str]:
        """获取聊天的 JID"""
        try:
            from neonize.utils import build_jid
            return build_jid(phone)
        except Exception as e:
            print(f"[Neonize] 构建 JID 失败: {e}")
            return None
    
    def _build_jid(self, phone: str) -> 'JID':
        """构建 JID 对象"""
        from neonize.utils import build_jid
        return build_jid(phone)
    
    def _lid_to_phone(self, lid: str) -> str:
        """将 LID 转换为手机号"""
        # 先检查缓存（问题3修复）
        if lid in self._lid_cache:
            return self._lid_cache[lid]
        
        conn = None
        try:
            # 查询 Neonize 数据库的 LID 映射表
            import sqlite3
            # 使用相对路径（问题3修复）
            db_path = os.path.join(os.path.dirname(__file__), "whatsapp_crm")
            conn = sqlite3.connect(db_path, timeout=5.0)  # 添加超时（问题3修复）
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT pn FROM whatsmeow_lid_map WHERE lid = ?",
                (lid,)
            )
            result = cursor.fetchone()
            
            if result and result[0]:
                # 存入缓存（问题3修复）
                self._lid_cache[lid] = result[0]
                return result[0]
        except Exception as e:
            logger.warning(f"[Neonize] LID 转换失败: {e}")
        finally:
            # 确保连接关闭（问题3修复）
            if conn:
                conn.close()
        
        # 如果转换失败，返回原始 LID
        return lid
    
    def send_message(self, jid: str, message: str, timeout: int = 15, max_retries: int = 2) -> bool:
        """发送消息 - 兼容 CLI 接口，接受 JID 格式，带超时保护和重试机制"""
        if not self.connected or not self.client:
            logger.warning(f"[Neonize] 未连接，无法发送消息到 {jid}")
            # 未连接时递增失败计数器
            with self._heartbeat_lock:
                self._consecutive_send_failures += 1
            return False
        
        # 标准化 phone 参数：如果是完整 JID，提取手机号部分
        if "@" in jid:
            phone = jid.split("@", 1)[0]
        else:
            phone = jid
        
        for attempt in range(max_retries + 1):
            try:
                from neonize.utils import build_jid
                jid_obj = build_jid(phone)
                # 使用类级别共享线程池（问题1修复）
                future = self._send_executor.submit(self.client.send_message, jid_obj, message=message)
                future.result(timeout=timeout)
                # 发送成功，重置失败计数器
                with self._heartbeat_lock:
                    self._consecutive_send_failures = 0
                logger.info(f"[Neonize] 消息已发送给 {phone}")
                print(f"[Neonize] 消息已发送给 {phone}")
                return True
            except concurrent.futures.TimeoutError:
                logger.error(f"[Neonize] 发送超时({timeout}s): {jid}, 尝试 {attempt+1}/{max_retries+1}")
            except Exception as e:
                logger.error(f"[Neonize] 发送失败: {e}, 尝试 {attempt+1}/{max_retries+1}")
            
            if attempt < max_retries:
                time.sleep(1)
        
        # 所有重试都失败，递增失败计数器
        with self._heartbeat_lock:
            self._consecutive_send_failures += 1
            current_failures = self._consecutive_send_failures
        
        # 如果连续失败次数达到阈值，记录 CRITICAL 日志
        if current_failures >= 3:
            logger.critical(f"[Neonize] 连续发送失败 {current_failures} 次，连接可能已失效，需要检查连接状态")
        
        return False
    
    def get_current_user(self) -> Optional[str]:
        """获取当前登录用户信息"""
        try:
            if self.client and self.client.me:
                # 从 JID 中提取手机号
                user_jid = self.client.me
                if hasattr(user_jid, 'User'):
                    return user_jid.User
                elif isinstance(user_jid, str):
                    return user_jid.split("@")[0] if "@" in user_jid else user_jid
            return None
        except Exception as e:
            logger.error(f"[Neonize] 获取当前用户失败: {e}")
            return None
    
    def send_image(self, phone: str, image_path: str, caption: str = "") -> bool:
        """发送图片"""
        if not self.connected or not self.client:
            logger.warning(f"[Neonize] 未连接，无法发送图片到 {phone}")
            return False
        
        try:
            jid = self._build_jid(phone)
            with open(image_path, 'rb') as f:
                image_data = f.read()
            
            response = self.client.send_image(jid, image_data, caption=caption)
            logger.info(f"[Neonize] 图片发送成功: {phone}")
            return True
        except Exception as e:
            logger.error(f"[Neonize] 发送图片失败: {e}")
            return False
    
    def send_video(self, phone: str, video_path: str, caption: str = "") -> bool:
        """发送视频"""
        if not self.connected or not self.client:
            logger.warning(f"[Neonize] 未连接，无法发送视频到 {phone}")
            return False
        
        try:
            jid = self._build_jid(phone)
            with open(video_path, 'rb') as f:
                video_data = f.read()
            
            response = self.client.send_video(jid, video_data, caption=caption)
            logger.info(f"[Neonize] 视频发送成功: {phone}")
            return True
        except Exception as e:
            logger.error(f"[Neonize] 发送视频失败: {e}")
            return False
    
    def send_document(self, phone: str, document_path: str, filename: str = None) -> bool:
        """发送文档"""
        if not self.connected or not self.client:
            logger.warning(f"[Neonize] 未连接，无法发送文档到 {phone}")
            return False
        
        try:
            jid = self._build_jid(phone)
            with open(document_path, 'rb') as f:
                doc_data = f.read()
            
            if not filename:
                import os
                filename = os.path.basename(document_path)
            
            response = self.client.send_document(jid, doc_data, filename=filename)
            logger.info(f"[Neonize] 文档发送成功: {phone}")
            return True
        except Exception as e:
            logger.error(f"[Neonize] 发送文档失败: {e}")
            return False
    
    def send_audio(self, phone: str, audio_path: str) -> bool:
        """发送音频"""
        if not self.connected or not self.client:
            logger.warning(f"[Neonize] 未连接，无法发送音频到 {phone}")
            return False
        
        try:
            jid = self._build_jid(phone)
            with open(audio_path, 'rb') as f:
                audio_data = f.read()
            
            response = self.client.send_audio(jid, audio_data)
            logger.info(f"[Neonize] 音频发送成功: {phone}")
            return True
        except Exception as e:
            logger.error(f"[Neonize] 发送音频失败: {e}")
            return False
    
    def get_user_info(self, phone: str) -> Optional[Dict]:
        """查询用户信息（是否注册 WhatsApp）"""
        if not self.connected or not self.client:
            logger.warning("[Neonize] 未连接，无法查询用户信息")
            return None
        
        try:
            # 使用 is_on_whatsapp 检查号码
            results = self.client.is_on_whatsapp(phone)
            
            if results and len(results) > 0:
                result = results[0]
                return {
                    "phone": phone,
                    "is_registered": result.IsIn,
                    "jid": result.JID.User if result.JID else None,
                    "is_business": result.IsBusiness if hasattr(result, 'IsBusiness') else False
                }
            
            return {"phone": phone, "is_registered": False}
            
        except Exception as e:
            logger.error(f"[Neonize] 查询用户信息失败: {e}")
            return None
    
    def set_status_message(self, status: str) -> bool:
        """设置 WhatsApp 状态消息"""
        if not self.connected or not self.client:
            logger.warning("[Neonize] 未连接，无法设置状态")
            return False
        
        try:
            self.client.set_status_message(status)
            logger.info(f"[Neonize] 状态已设置: {status[:30]}...")
            return True
        except Exception as e:
            logger.error(f"[Neonize] 设置状态失败: {e}")
            return False
    
    def get_status_message(self) -> Optional[str]:
        """获取当前状态消息"""
        if not self.connected or not self.client:
            logger.warning("[Neonize] 未连接，无法获取状态")
            return None
        
        try:
            # Neonize 没有直接获取状态的方法，需要通过其他方式
            # 这里返回一个提示
            return "请通过 WhatsApp 应用查看当前状态"
        except Exception as e:
            logger.error(f"[Neonize] 获取状态失败: {e}")
            return None
    
    def set_profile_name(self, name: str) -> bool:
        """设置 WhatsApp 显示名称"""
        if not self.connected or not self.client:
            logger.warning("[Neonize] 未连接，无法设置名称")
            return False
        
        try:
            self.client.set_profile_name(name)
            logger.info(f"[Neonize] 名称已设置: {name}")
            return True
        except Exception as e:
            logger.error(f"[Neonize] 设置名称失败: {e}")
            return False
    
    def get_contacts(self) -> List[Dict]:
        """获取联系人列表 - Neonize 暂不支持，返回空列表"""
        logger.warning("[Neonize] get_contacts 暂不支持，返回空列表")
        return []
    
    def get_chats(self) -> List[Dict]:
        """获取聊天列表 - Neonize 暂不支持，返回空列表"""
        logger.warning("[Neonize] get_chats 暂不支持，返回空列表")
        return []
    
    def is_authenticated(self) -> bool:
        """检查是否已认证"""
        return self.connected
    
    def disconnect(self):
        """断开连接"""
        # 停止消息队列工作线程
        self._queue_running = False
        if self._queue_worker:
            self._queue_worker.join(timeout=2)
            print("[Neonize] 消息队列处理线程已停止")
        
        # 关闭共享线程池（问题6修复）
        if hasattr(self, '_send_executor') and self._send_executor:
            self._send_executor.shutdown(wait=False)
            print("[Neonize] 发送线程池已关闭")
        
        # 清理消息缓存（问题6修复）
        with self._msg_id_lock:
            self._received_message_ids.clear()
        print("[Neonize] 消息ID缓存已清理")
        
        # 清理LID缓存（问题6修复）
        self._lid_cache.clear()
        print("[Neonize] LID缓存已清理")
        
        # 设置client为None（问题6修复）
        self.client = None
        self.connected = False
        print("[Neonize] 客户端已断开")
    
    def is_connected(self) -> bool:
        """检查当前连接状态"""
        return self.connected
    
    def get_qr_code(self) -> Optional[str]:
        """获取登录二维码"""
        return get_current_qr()
    
    @property
    def backend_name(self) -> str:
        """后端名称标识"""
        return "neonize"
    
    def connect_with_retry(self, max_retries: int = 5):
        """带指数退避重试的连接方法"""
        for attempt in range(max_retries):
            try:
                logger.info(f"[Neonize] 第 {attempt+1}/{max_retries} 次连接尝试")
                self.connect()
                
                # connect() 内部启动了后台线程，等待一段时间检查连接结果
                # 给予足够时间让二维码扫描或自动登录完成
                wait_time = 30  # 等待30秒看是否连接成功
                for _ in range(wait_time):
                    if self.connected:
                        logger.info("[Neonize] 连接成功!")
                        return True
                    time.sleep(1)
                
                # 如果30秒后仍未连接（可能在等待二维码扫描）
                # 不应该视为失败重试，而是继续等待
                logger.info("[Neonize] 等待用户扫描二维码...")
                # 持续等待直到连接或超时
                for _ in range(270):  # 再等 4.5 分钟
                    if self.connected:
                        logger.info("[Neonize] 连接成功!")
                        return True
                    time.sleep(1)
                
                logger.warning(f"[Neonize] 第 {attempt+1} 次尝试超时")
                
            except Exception as e:
                logger.error(f"[Neonize] 连接异常: {e}")
            
            if attempt < max_retries - 1:
                wait = min(2 ** attempt, 30)
                logger.info(f"[Neonize] {wait}秒后重试...")
                time.sleep(wait)
        
        logger.error(f"[Neonize] 已重试 {max_retries} 次，连接失败")
        return False


# 保持与旧版兼容的接口
class MessageSyncer:
    """消息同步器 - 兼容旧版接口"""
    
    def __init__(self, client: NeonizeWhatsAppClient):
        self.client = client
        self.is_running = False
    
    def start_polling(self, interval: int = 1):
        """开始轮询 - Neonize 使用事件驱动，无需轮询"""
        self.is_running = True
        print("[MessageSyncer] Neonize 使用事件驱动，无需轮询")
        return asyncio.Future()  # 返回一个永不完成的 Future
    
    def stop(self):
        """停止同步"""
        self.is_running = False


# 全局客户端实例
_whatsapp_client: Optional[NeonizeWhatsAppClient] = None

def get_whatsapp_client() -> Optional[NeonizeWhatsAppClient]:
    """获取全局 WhatsApp 客户端实例"""
    global _whatsapp_client
    return _whatsapp_client

def init_whatsapp_client() -> NeonizeWhatsAppClient:
    """初始化 WhatsApp 客户端"""
    global _whatsapp_client
    if _whatsapp_client is None:
        _whatsapp_client = NeonizeWhatsAppClient()
    return _whatsapp_client
