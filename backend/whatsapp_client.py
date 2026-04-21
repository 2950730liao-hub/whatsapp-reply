"""
WhatsApp CLI 客户端封装
"""
import subprocess
import json
import os
import asyncio
from typing import Optional, List, Dict, Callable
from datetime import datetime
from database import SessionLocal, Customer, Message, Conversation, CustomerTag, CustomerTagAssociation
from whatsapp_interface import IWhatsAppClient


class WhatsAppClient(IWhatsAppClient):
    """WhatsApp CLI 封装类"""
    
    def __init__(self, cli_path: str = "whatsapp", store_dir: Optional[str] = None):
        self.cli_path = cli_path
        self.store_dir = store_dir or os.path.expanduser("~/.config/whatsapp-cli")
        self._ensure_path()
        self._sync_process = None
    
    def _ensure_path(self):
        """确保 whatsapp-cli 在 PATH 中"""
        whatsapp_bin = os.path.expanduser("~/.local/bin")
        if whatsapp_bin not in os.environ.get("PATH", ""):
            os.environ["PATH"] = f"{whatsapp_bin}:{os.environ.get('PATH', '')}"
    
    def start_sync_daemon(self):
        """启动 whatsapp sync --follow 后台进程保持连接"""
        import subprocess
        try:
            # 检查是否已有 sync 进程在运行
            result = subprocess.run(
                ["pgrep", "-f", "whatsapp sync --follow"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                print("[WhatsApp] sync 进程已在运行")
                return True
            
            # 启动新的 sync 进程
            print("[WhatsApp] 启动 sync --follow 进程...")
            self._sync_process = subprocess.Popen(
                [self.cli_path, "sync", "--follow"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            print(f"[WhatsApp] sync 进程已启动 (PID: {self._sync_process.pid})")
            return True
        except Exception as e:
            print(f"[WhatsApp] 启动 sync 进程失败: {e}")
            return False
    
    def stop_sync_daemon(self):
        """停止 sync 后台进程"""
        if self._sync_process:
            self._sync_process.terminate()
            self._sync_process = None
            print("[WhatsApp] sync 进程已停止")
    
    async def _run_async(self, cmd: List[str], timeout: int = 30) -> dict:
        """异步执行 whatsapp 命令"""
        full_cmd = [self.cli_path] + cmd + ["--format", "json"]
        
        process = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            raise RuntimeError(f"命令超时: {' '.join(cmd)}")
        
        if process.returncode != 0:
            error_msg = stderr.decode().strip() if stderr else "未知错误"
            raise RuntimeError(f"命令失败: {' '.join(cmd)} - {error_msg}")
        
        output = stdout.decode().strip()
        if not output:
            return {}
        
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {"raw_output": output}
    
    def _run_sync(self, cmd: List[str], timeout: int = 30) -> dict:
        """同步执行 whatsapp 命令"""
        full_cmd = [self.cli_path] + cmd + ["--format", "json"]
        
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else "未知错误"
            raise RuntimeError(f"命令失败: {' '.join(cmd)} - {error_msg}")
        
        if not result.stdout.strip():
            return {}
        
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"raw_output": result.stdout}
    
    def auth_status(self) -> dict:
        """检查登录状态"""
        return self._run_sync(["auth", "status"])
    
    def is_authenticated(self) -> bool:
        """检查是否已认证"""
        try:
            status = self.auth_status()
            return status.get("connected", False)
        except:
            return False
    
    def get_chats(self, groups_only: bool = False, query: Optional[str] = None) -> List[Dict]:
        """获取聊天列表"""
        cmd = ["chats"]
        if groups_only:
            cmd.append("--groups")
        if query:
            cmd.extend(["--query", query])
        
        result = self._run_sync(cmd)
        return result if isinstance(result, list) else []
    
    def get_contacts(self) -> List[Dict]:
        """获取联系人列表"""
        result = self._run_sync(["contacts"])
        return result if isinstance(result, list) else []
    
    def logout(self) -> bool:
        """退出登录"""
        try:
            self._run_sync(["auth", "logout"])
            return True
        except Exception as e:
            print(f"退出登录失败: {e}")
            return False
    
    def get_current_user(self) -> Optional[str]:
        """获取当前登录的用户手机号"""
        try:
            status = self.auth_status()
            device = status.get("device", {})
            return device.get("user")
        except:
            return None
    
    def get_messages(self, jid: str, limit: int = 50) -> List[Dict]:
        """获取消息列表"""
        result = self._run_sync(["messages", jid, "--limit", str(limit)])
        return result if isinstance(result, list) else []
    
    def send_message(self, jid: str, message: str, **kwargs) -> bool:
        """发送消息 - 自动处理JID格式，支持多种后缀"""
        import logging
        logger = logging.getLogger(__name__)
        
        # 支持的JID后缀列表（按优先级排序）
        SUPPORTED_SUFFIXES = ["@s.whatsapp.net", "@lid", "@g.us", "@broadcast"]
        
        try:
            # 首先尝试使用提供的JID发送
            try:
                logger.info(f"[发送消息] 尝试使用原始JID发送: {jid}")
                self._run_sync(["send", jid, message])
                logger.info(f"[发送消息] 使用原始JID发送成功: {jid}")
                return True
            except Exception as e1:
                logger.warning(f"[发送消息] 使用原始JID发送失败: {jid}, 错误: {e1}")
                
                # 检查JID是否包含已知的后缀
                has_known_suffix = any(suffix in jid for suffix in SUPPORTED_SUFFIXES)
                
                if has_known_suffix:
                    # 尝试切换JID后缀
                    alt_jid = None
                    if "@s.whatsapp.net" in jid:
                        alt_jid = jid.replace("@s.whatsapp.net", "@lid")
                    elif "@lid" in jid:
                        alt_jid = jid.replace("@lid", "@s.whatsapp.net")
                    
                    if alt_jid:
                        logger.info(f"[发送消息] 使用备用JID重试: {alt_jid}")
                        print(f"[发送消息] 使用备用JID重试: {alt_jid}")
                        self._run_sync(["send", alt_jid, message])
                        logger.info(f"[发送消息] 使用备用JID发送成功: {alt_jid}")
                        return True
                    else:
                        # 其他后缀（如@g.us、@broadcast）没有备用格式，直接抛出原始错误
                        raise e1
                else:
                    # 无法识别的JID格式
                    logger.error(f"[发送消息] 无法识别的JID格式: {jid}，不支持的后缀")
                    print(f"[发送消息] 警告: 无法识别的JID格式 '{jid}'，请使用标准格式（如: 86138xxx@s.whatsapp.net）")
                    raise RuntimeError(f"无法识别的JID格式: {jid}，请使用标准格式（如: 86138xxx@s.whatsapp.net, 86138xxx@lid, xxx@g.us）")
        except Exception as e:
            logger.error(f"[发送消息] 发送失败: {e}")
            print(f"[发送消息] 发送失败: {e}")
            return False
    
    def get_chat_jid(self, phone: str) -> Optional[str]:
        """根据手机号获取正确的聊天JID"""
        try:
            # 获取所有聊天
            chats = self.get_chats()
            
            # 首先精确匹配
            for chat in chats:
                jid = chat.get("jid", "")
                if phone in jid:
                    return jid
            
            # 如果没有找到，尝试使用默认格式
            return f"{phone}@s.whatsapp.net"
        except Exception as e:
            print(f"获取聊天JID失败: {e}")
            return None
    
    async def sync_messages_continuously(self, callback: Callable):
        """
        持续同步消息（使用 whatsapp sync --follow）
        实时接收新消息
        """
        import asyncio
        import subprocess
        
        print("[实时同步] 启动 whatsapp sync --follow...")
        
        process = await asyncio.create_subprocess_exec(
            self.cli_path, "sync", "--follow",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            while True:
                # 读取输出
                line = await process.stdout.readline()
                if not line:
                    break
                
                line_str = line.decode().strip()
                if line_str:
                    print(f"[实时同步] {line_str}")
                    # 检测到新消息时触发回调
                    if "new message" in line_str.lower() or "message" in line_str.lower():
                        await callback()
                        
        except asyncio.CancelledError:
            print("[实时同步] 停止实时同步")
            process.terminate()
        except Exception as e:
            print(f"[实时同步] 错误: {e}")
            process.terminate()


class MessageSyncer:
    """消息同步器 - 将 WhatsApp 消息同步到数据库"""
    
    def __init__(self, whatsapp_client: WhatsAppClient):
        self.client = whatsapp_client
        self.known_message_ids = set()
        self.is_running = False
    
    def _get_or_create_customer(self, db, phone: str, name: str = None) -> Customer:
        """获取或创建客户"""
        # 清理电话号码格式
        phone = phone.split("@")[0]  # 移除 @s.whatsapp.net
        
        customer = db.query(Customer).filter(Customer.phone == phone).first()
        if not customer:
            customer = Customer(
                phone=phone,
                name=name or phone,
                category="new"
            )
            db.add(customer)
            db.commit()
            db.refresh(customer)
            
            # 为新客户自动打上"新客户"标签
            self._apply_new_customer_tag(db, customer.id)
            
        return customer
    
    def _apply_new_customer_tag(self, db, customer_id: int):
        """为新客户自动打上'新客户'标签"""
        try:
            # 查找名为"新客户"的标签
            new_customer_tag = db.query(CustomerTag).filter(
                CustomerTag.name == "新客户"
            ).first()
            
            if new_customer_tag:
                # 检查是否已经有这个标签
                existing = db.query(CustomerTagAssociation).filter(
                    CustomerTagAssociation.customer_id == customer_id,
                    CustomerTagAssociation.tag_id == new_customer_tag.id
                ).first()
                
                if not existing:
                    # 添加标签
                    association = CustomerTagAssociation(
                        customer_id=customer_id,
                        tag_id=new_customer_tag.id
                    )
                    db.add(association)
                    db.commit()
                    print(f"[新客户标签] 已为客户 {customer_id} 自动打上'新客户'标签")
        except Exception as e:
            print(f"[新客户标签] 应用失败: {e}")
    
    def _get_or_create_conversation(self, db, customer_id: int) -> Conversation:
        """获取或创建会话"""
        conversation = db.query(Conversation).filter(
            Conversation.customer_id == customer_id,
            Conversation.status.in_(["bot", "handover"])
        ).first()
        
        if not conversation:
            conversation = Conversation(
                customer_id=customer_id,
                status="bot"
            )
            db.add(conversation)
            db.commit()
            db.refresh(conversation)
        
        return conversation
    
    def sync_chat_messages(self, jid: str, db) -> List[Message]:
        """同步单个聊天的消息"""
        messages_data = self.client.get_messages(jid, limit=20)
        new_messages = []
        
        for msg_data in messages_data:
            wa_msg_id = msg_data.get("id")
            
            # 跳过已知消息
            if wa_msg_id in self.known_message_ids:
                continue
            
            # 检查数据库中是否已存在
            existing = db.query(Message).filter(
                Message.wa_message_id == wa_msg_id
            ).first()
            if existing:
                self.known_message_ids.add(wa_msg_id)
                continue
            
            # 获取发送者信息
            sender_jid = msg_data.get("sender_jid", "")
            is_from_me = msg_data.get("from_me", False)
            
            # 确定客户电话
            if is_from_me:
                # 自己发送的消息，客户是接收方
                customer_phone = jid.split("@")[0]
            else:
                # 收到的消息，客户是发送方
                customer_phone = sender_jid.split("@")[0] if sender_jid else jid.split("@")[0]
            
            # 获取或创建客户
            sender_name = msg_data.get("sender_name") or msg_data.get("push_name") or customer_phone
            customer = self._get_or_create_customer(db, customer_phone, sender_name)
            
            # 获取或创建会话
            conversation = self._get_or_create_conversation(db, customer.id)
            
            # 创建消息记录
            message = Message(
                customer_id=customer.id,
                wa_message_id=wa_msg_id,
                content=msg_data.get("content", ""),
                direction="outgoing" if is_from_me else "incoming",
                sender_name=sender_name,
                message_type=msg_data.get("type", "text"),
                is_read=is_from_me  # 自己发送的标记为已读
            )
            db.add(message)
            
            # 更新会话最后消息时间
            conversation.last_message_at = datetime.utcnow()
            
            db.commit()
            
            self.known_message_ids.add(wa_msg_id)
            new_messages.append(message)
        
        return new_messages
    
    def sync_all_chats(self, db) -> Dict[str, List[Message]]:
        """同步所有聊天的消息"""
        chats = self.client.get_chats()
        all_new_messages = {}
        
        for chat in chats:
            jid = chat.get("jid")
            if not jid:
                continue
            
            try:
                new_messages = self.sync_chat_messages(jid, db)
                if new_messages:
                    all_new_messages[jid] = new_messages
            except Exception as e:
                print(f"同步聊天 {jid} 失败: {e}")
        
        return all_new_messages
    
    async def start_polling(self, interval: int = 1):
        """开始轮询同步 - 默认1秒间隔以实现近实时同步"""
        self.is_running = True
        print(f"开始消息轮询，间隔 {interval} 秒...")
        
        last_sync_time = 0
        
        while self.is_running:
            try:
                current_time = asyncio.get_event_loop().time()
                
                # 限制最小同步间隔，避免过于频繁
                if current_time - last_sync_time >= interval:
                    db = SessionLocal()
                    new_messages = self.sync_all_chats(db)
                    
                    if new_messages:
                        total = sum(len(msgs) for msgs in new_messages.values())
                        print(f"[实时同步] 同步了 {total} 条新消息")
                        
                        # 处理新消息（自动回复等）
                        await self._process_new_messages(new_messages, db)
                    
                    db.close()
                    last_sync_time = current_time
                else:
                    # 等待一小段时间再检查
                    await asyncio.sleep(0.1)
                    
            except Exception as e:
                print(f"轮询错误: {e}")
                await asyncio.sleep(interval)
    
    async def _process_new_messages(self, new_messages: Dict[str, List[Message]], db):
        """处理新消息 - 触发自动回复"""
        from communication_service import CommunicationService
        from communication_service import NotificationService
        
        for jid, messages in new_messages.items():
            for msg in messages:
                if msg.direction == "incoming":
                    # 获取客户
                    customer = db.query(Customer).filter(
                        Customer.id == msg.customer_id
                    ).first()
                    
                    if customer:
                        print(f"[消息处理] 收到来自 {customer.phone} 的消息: {msg.content[:30]}...")
                        
                        # 处理消息（自动回复）
                        comm_service = CommunicationService(db, self.client)
                        need_notify = comm_service.handle_incoming_message(msg, customer)
                        
                        print(f"[消息处理] 需要通知人工: {need_notify}")
                        
                        # 通知人工
                        if need_notify:
                            conversation = db.query(Conversation).filter(
                                Conversation.customer_id == customer.id,
                                Conversation.status.in_(["bot", "handover"])
                            ).first()
                            
                            if conversation:
                                notification_service = NotificationService(db, self.client)
                                notification_service.notify_agents_new_message(
                                    customer, msg, conversation
                                )
    
    def stop(self):
        """停止轮询"""
        self.is_running = False

    def connect(self) -> bool:
        """连接 WhatsApp 服务
        
        CLI 后端使用 sync 守护进程保持连接
        """
        return self.start_sync_daemon()
    
    def disconnect(self) -> None:
        """断开连接"""
        self.stop_sync_daemon()
    
    def is_connected(self) -> bool:
        """检查当前连接状态"""
        return self.is_authenticated()
    
    def on_message(self, handler: Callable) -> None:
        """注册消息接收处理器
        
        CLI 后端通过 MessageSyncer 处理消息，此方法仅作兼容
        """
        # CLI 后端使用轮询方式，处理器在 MessageSyncer 中调用
        pass
    
    def get_qr_code(self) -> Optional[str]:
        """获取登录二维码
        
        CLI 后端通过命令行交互获取二维码，此方法返回 None
        实际二维码获取需通过 whatsapp auth login 命令
        """
        return None
    
    @property
    def backend_name(self) -> str:
        """后端名称标识"""
        return "cli"
