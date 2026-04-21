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
from typing import Optional, List, Dict, Callable
from datetime import datetime
from database import SessionLocal, Customer, Message, Conversation

logger = logging.getLogger(__name__)

# 延迟导入 neonize，避免启动时加载
try:
    from neonize.client import NewClient
    from neonize.events import (
        MessageEv, ConnectedEv, DisconnectedEv, QREv, 
        ConnectFailureEv, LoggedOutEv, event
    )
    NEONIZE_AVAILABLE = True
except ImportError:
    NEONIZE_AVAILABLE = False
    print("[Neonize] 未安装，请运行: pip install neonize")


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


class NeonizeWhatsAppClient:
    """基于 Neonize 的 WhatsApp 客户端"""
    
    def __init__(self, name: str = "whatsapp_crm"):
        self.name = name
        self.client: Optional[NewClient] = None
        self.connected = False
        self.message_handlers: List[Callable] = []
        self._handlers_lock = threading.Lock()
        
        # 消息去重机制
        self._received_message_ids = set()
        self._msg_id_lock = threading.Lock()
        self._max_cached_ids = 10000  # 最大缓存数
        
        # 消息队列机制
        self._message_queue = queue.Queue(maxsize=1000)
        self._queue_worker = None
        self._queue_running = False
        
        if not NEONIZE_AVAILABLE:
            raise RuntimeError("Neonize 未安装")
    
    def on_message(self, handler: Callable):
        """注册消息处理器"""
        with self._handlers_lock:
            self.message_handlers.append(handler)
    
    def _handle_incoming_message(self, client: NewClient, event: MessageEv):
        """处理收到的消息 - 快速入队模式"""
        # 消息去重检查
        msg_id = str(event.Info.ID) if event.Info.ID else None
        if msg_id:
            with self._msg_id_lock:
                if msg_id in self._received_message_ids:
                    logger.debug(f"[Neonize] 跳过重复消息: {msg_id}")
                    return
                self._received_message_ids.add(msg_id)
                # 防止内存泄漏：超过上限清理旧 ID
                if len(self._received_message_ids) > self._max_cached_ids:
                    # 清空一半（简单策略）
                    to_remove = list(self._received_message_ids)[:self._max_cached_ids // 2]
                    for rid in to_remove:
                        self._received_message_ids.discard(rid)
        
        # 快速入队
        try:
            self._message_queue.put_nowait(event)
            logger.debug(f"[Neonize] 消息已入队，当前队列大小: {self._message_queue.qsize()}")
        except queue.Full:
            logger.error("[Neonize] 消息队列已满，消息丢失！")
    
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
            
            # 提取手机号（JID格式：123456789@s.whatsapp.net）
            # sender_jid 可能是 JID 对象或字符串
            if hasattr(sender_jid, 'User'):
                # 是 JID 对象
                sender_phone = sender_jid.User
            elif isinstance(sender_jid, str):
                # 是字符串
                sender_phone = sender_jid.split("@")[0] if sender_jid else ""
            else:
                sender_phone = str(sender_jid) if sender_jid else ""
            
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
        
        # 注册事件处理器
        self.client.event(ConnectedEv)(self._on_connected)
        self.client.event(DisconnectedEv)(self._on_disconnected)
        self.client.event(MessageEv)(self._handle_incoming_message)
        self.client.event(ConnectFailureEv)(self._on_connect_failure)
        self.client.event(LoggedOutEv)(self._on_logged_out)
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
    
    def send_message(self, jid: str, message: str, timeout: int = 15, max_retries: int = 2) -> bool:
        """发送消息 - 兼容 CLI 接口，接受 JID 格式，带超时保护和重试机制"""
        if not self.connected or not self.client:
            logger.warning(f"[Neonize] 未连接，无法发送消息到 {jid}")
            return False
        
        from neonize.utils import build_jid
        
        # 标准化 phone 参数：如果是完整 JID，提取手机号部分
        if "@" in jid:
            phone = jid.split("@", 1)[0]
        else:
            phone = jid
        
        for attempt in range(max_retries + 1):
            try:
                jid_obj = build_jid(phone)
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(self.client.send_message, jid_obj, message=message)
                    future.result(timeout=timeout)
                logger.info(f"[Neonize] 消息已发送给 {phone}")
                print(f"[Neonize] 消息已发送给 {phone}")
                return True
            except concurrent.futures.TimeoutError:
                logger.error(f"[Neonize] 发送超时({timeout}s): {jid}, 尝试 {attempt+1}/{max_retries+1}")
            except Exception as e:
                logger.error(f"[Neonize] 发送失败: {e}, 尝试 {attempt+1}/{max_retries+1}")
            
            if attempt < max_retries:
                time.sleep(1)
        
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
        
        if self.client:
            # Neonize 没有显式的 disconnect 方法
            pass
    
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
