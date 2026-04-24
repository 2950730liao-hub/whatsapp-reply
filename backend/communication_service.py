"""
沟通计划服务 - 处理自动回复和沟通计划
"""
import asyncio
import logging
import os
from typing import Optional
from datetime import datetime, timedelta
from contextlib import contextmanager
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import (
    Customer, Message, Conversation, CommunicationPlan, 
    PlanExecution, Agent, AutoTagRule, CustomerTagAssociation, CustomerTagLog,
    SessionLocal
)

logger = logging.getLogger(__name__)

# 客户端类型导入（用于类型检查）
try:
    from neonize_client import NeonizeWhatsAppClient
except ImportError:
    from whatsapp_client import WhatsAppClient

from llm_service import get_llm_service
from knowledge_base import get_knowledge_base

# 商机通知服务
try:
    from notify_service import get_notify_service
except ImportError:
    get_notify_service = None


class CommunicationService:
    """沟通服务 - 管理自动回复和沟通计划"""
    
    # 转人工关键词列表（支持中英文）
    TRANSFER_KEYWORDS = [
        # 中文
        "人工", "客服", "转人工", "找人工", "人工客服", "人工服务",
        "真人", "找真人", "转接", "转接客服", "人工客服",
        # 英文
        "agent", "human", "help", "operator", "real person",
        "transfer", "live chat", "live agent", "support",
        "customer service", "talk to human", "speak to agent",
    ]
    
    # 客户分类对应的自动回复模板
    AUTO_REPLY_TEMPLATES = {
        "new": """您好！感谢您联系我们 🎉

我是小芃科技的智能客服，已经收到您的消息。

我们的销售专员会尽快与您联系，请稍候...

【紧急咨询请回复"人工"】""",
        
        "lead": """您好！欢迎再次咨询 👋

我是您的专属智能助手，已记录您的需求。

销售专员正在查看您的消息，马上为您服务！""",
        
        "returning": """欢迎回来！🌟

感谢您一直以来对小芃科技的支持。

您的专属客服正在为您准备服务，请稍候..."""
    }
    
    def __init__(self, db: Optional[Session], whatsapp_client):
        self.db = db
        self.wa_client = whatsapp_client
    
    @contextmanager
    def _get_db(self):
        """获取数据库会话（上下文管理器模式）"""
        db = self.db if self.db is not None else SessionLocal()
        try:
            yield db
        finally:
            if self.db is None:
                db.close()
    
    def _mark_message_processed(self, message_id: int):
        """标记消息为已处理（AI 已回复）"""
        try:
            with self._get_db() as db:
                msg = db.query(Message).filter(Message.id == message_id).first()
                if msg:
                    msg.is_processed = True
                    db.commit()
                    print(f"[MessageProcess] 消息 {message_id} 标记为已处理")
        except Exception as e:
            logger.warning(f"[MessageProcess] 标记消息已处理失败: {e}")
    
    def handle_incoming_message(self, message: Message, customer: Customer) -> bool:
        """
        处理收到的消息
        返回是否需要通知人工
        """
        content_preview = message.content[:20] + "..." if len(message.content) > 20 else message.content
        print(f"[handle_incoming_message] 开始处理消息: {content_preview}")
        print(f"[handle_incoming_message] 客户分类: {customer.category}")
        
        # 获取或创建会话
        conversation = self._get_active_conversation(customer.id)
        print(f"[handle_incoming_message] 会话状态: {conversation.status}")
        
        # 检查是否是转人工请求
        if self._is_handover_request(message.content):
            print(f"[handle_incoming_message] 识别为转人工请求")
            return self._handle_handover_request(conversation, customer, message)
        
        # 如果会话已被人工接手，不自动回复
        if conversation.status == "handover":
            print(f"[handle_incoming_message] 会话已被人工接手")
            return True  # 通知人工有新消息
        
        # 商机通知检测
        self._check_and_notify(customer, message)
        
        # 根据客户分类处理
        print(f"[handle_incoming_message] 根据分类处理: {customer.category}")
        if customer.category == "new":
            return self._handle_new_customer(customer, conversation, message)
        elif customer.category == "lead":
            return self._handle_lead_customer(customer, conversation, message)
        elif customer.category == "returning":
            return self._handle_returning_customer(customer, conversation, message)
        
        print(f"[handle_incoming_message] 未知分类，不处理")
        return False
    
    def _check_and_notify(self, customer: Customer, message: Message):
        """商机检测 + 通知管理员（异步执行）"""
        if not get_notify_service:
            return
        
        import threading
        
        def notify_async():
            try:
                # 判断是否首次回复
                with self._get_db() as db:
                    prev_count = db.query(Message).filter(
                        Message.customer_id == customer.id,
                        Message.direction == "incoming",
                        Message.id != message.id
                    ).count()
                is_first = (prev_count == 0)
                
                notify_svc = get_notify_service()
                notify_svc.set_client(self.wa_client)
                notify_svc.check_and_notify(
                    customer_phone=customer.phone,
                    customer_name=customer.name or customer.phone,
                    message_content=message.content,
                    is_first_message=is_first
                )
            except Exception as e:
                logger.error(f"[商机通知] 异常: {e}")
        
        thread = threading.Thread(target=notify_async, daemon=True)
        thread.start()
    
    def _get_active_conversation(self, customer_id: int) -> Conversation:
        """获取活跃会话"""
        with self._get_db() as db:
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
    
    def _is_handover_request(self, content: str) -> bool:
        """检查是否是转人工请求（支持中英文，不区分大小写）"""
        if not content:
            return False
        content_lower = content.lower()
        return any(keyword in content_lower for keyword in self.TRANSFER_KEYWORDS)
    
    def _handle_handover_request(self, conversation: Conversation, 
                                  customer: Customer, message: Message) -> bool:
        """处理转人工请求"""
        # 更新会话状态 - 在新Session中重新查询conversation以避免脱管对象问题
        with self._get_db() as db:
            db_conv = db.query(Conversation).filter(
                Conversation.id == conversation.id
            ).first()
            if db_conv:
                db_conv.status = "handover"
                db.commit()
        
        # 发送确认消息
        reply_text = "已为您转接人工客服，请稍候..."
        self._send_reply(customer.phone, reply_text)
        
        # 记录转人工消息
        self._record_outgoing_message(customer.id, reply_text)
        
        return True  # 需要通知人工
    
    def _handle_new_customer(self, customer: Customer, 
                             conversation: Conversation, 
                             message: Message) -> bool:
        """处理新客户 - 使用AI智能回复"""
        content_preview = message.content[:20] + "..." if len(message.content) > 20 else message.content
        print(f"[处理新客户] 客户: {customer.phone}, 消息: {content_preview}")
        
        # 检查是否已经发送过欢迎语
        with self._get_db() as db:
            has_welcome = db.query(Message).filter(
                Message.customer_id == customer.id,
                Message.direction == "outgoing"
            ).first()
        
        print(f"[处理新客户] 已有欢迎语: {has_welcome is not None}")
        
        if not has_welcome:
            # 发送欢迎语
            welcome_text = self.AUTO_REPLY_TEMPLATES["new"]
            self._send_reply(customer.phone, welcome_text)
            self._record_outgoing_message(customer.id, welcome_text)
            print(f"[处理新客户] 已发送欢迎语")
        else:
            # 已发过欢迎语，使用AI智能回复
            print(f"[处理新客户] 调用AI智能回复...")
            self._send_ai_reply_sync(customer, message)
        
        # 新客户需要通知人工
        return True
    
    def _handle_lead_customer(self, customer: Customer,
                              conversation: Conversation,
                              message: Message) -> bool:
        """处理意向客户 - 使用AI智能回复"""
        # 检查是否需要自动回复
        with self._get_db() as db:
            recent_outgoing = db.query(Message).filter(
                Message.customer_id == customer.id,
                Message.direction == "outgoing"
            ).order_by(Message.created_at.desc()).first()
        
        # 如果最近 5 分钟内没有自动回复，则使用AI回复
        if not recent_outgoing or \
           (datetime.utcnow() - recent_outgoing.created_at) > timedelta(minutes=5):
            # 使用同步方式调用AI回复
            self._send_ai_reply_sync(customer, message)
        
        return True
    
    def _handle_returning_customer(self, customer: Customer,
                                   conversation: Conversation,
                                   message: Message) -> bool:
        """处理老客户 - 使用AI智能回复"""
        # 检查是否需要自动回复
        with self._get_db() as db:
            recent_outgoing = db.query(Message).filter(
                Message.customer_id == customer.id,
                Message.direction == "outgoing"
            ).order_by(Message.created_at.desc()).first()
        
        # 如果最近 10 分钟内没有自动回复，则使用AI回复
        if not recent_outgoing or \
           (datetime.utcnow() - recent_outgoing.created_at) > timedelta(minutes=10):
            # 使用同步方式调用AI回复
            self._send_ai_reply_sync(customer, message)
        
        return True
    
    def _send_ai_reply_sync(self, customer: Customer, incoming_msg: Message):
        """发送AI智能回复（同步版本）"""
        print(f"[_send_ai_reply_sync] 开始生成AI回复...")
        max_retries = 2
        retry_count = 0
        
        while retry_count <= max_retries:
            try:
                # 获取历史消息 - 在with块内提取所有需要的属性为普通Python变量
                with self._get_db() as db:
                    messages = db.query(Message).filter(
                        Message.customer_id == customer.id
                    ).order_by(Message.created_at.desc()).limit(10).all()
                    # 将ORM对象转换为普通dict，避免离开with块后lazy load失败
                    messages_data = []
                    for msg in messages:
                        messages_data.append({
                            'id': msg.id,
                            'customer_id': msg.customer_id,
                            'content': msg.content,
                            'direction': msg.direction,
                            'sender_name': msg.sender_name,
                            'is_read': msg.is_read,
                            'created_at': msg.created_at
                        })
                    # 提取客户属性
                    customer_data = {
                        'id': customer.id,
                        'phone': customer.phone,
                        'name': customer.name,
                        'category': customer.category,
                        'status': customer.status,
                        'created_at': customer.created_at
                    }
                
                # 获取相关知识（全局知识库）
                kb = get_knowledge_base()
                knowledge = kb.get_relevant_knowledge(incoming_msg.content)
                
                # 获取智能体绑定的知识库
                def get_agent_knowledge(msg_content):
                    from database import SessionLocal, AgentKnowledgeBinding
                    _db = SessionLocal()
                    try:
                        llm_svc = get_llm_service()
                        _agent = llm_svc.get_agent_for_customer(customer_data, _db)
                        print(f"[get_agent_knowledge] 智能体: {_agent.name if _agent else 'None'}")
                        if _agent:
                            bindings = _db.query(AgentKnowledgeBinding).filter(
                                AgentKnowledgeBinding.agent_id == _agent.id
                            ).all()
                            print(f"[get_agent_knowledge] 绑定数量: {len(bindings)}")
                            if bindings:
                                doc_ids = [b.knowledge_doc_id for b in bindings]
                                print(f"[get_agent_knowledge] 文档IDs: {doc_ids}")
                                agent_kb_content = kb.get_documents_by_ids(doc_ids)
                                print(f"[get_agent_knowledge] 知识库长度: {len(agent_kb_content) if agent_kb_content else 0}")
                                if agent_kb_content:
                                    return agent_kb_content
                    except Exception as e:
                        print(f"[get_agent_knowledge] 异常: {e}")
                        import traceback
                        traceback.print_exc()
                    finally:
                        _db.close()
                    return None
                
                agent_knowledge = get_agent_knowledge(incoming_msg.content)
                # 智能体知识库优先，如果没有则用全局知识库
                final_knowledge = agent_knowledge if agent_knowledge else knowledge
                
                # 获取所有知识库文档的附件（用于附件发送）
                all_kb_attachments = kb.get_all_attachments()
                print(f"[_send_ai_reply_sync] 所有知识库附件数量: {len(all_kb_attachments)}")
                for att in all_kb_attachments:
                    print(f"  - {att['name']}: {att['file_path'][:50]}...")
                
                # 注入 AI 记忆经验
                try:
                    from ai_memory import get_communication_tips
                    memory_tips = get_communication_tips(limit=3)
                    if memory_tips:
                        final_knowledge = (final_knowledge + "\n\n" + memory_tips).strip() if final_knowledge else memory_tips
                except Exception:
                    pass
                
                # 检查客户是否在询问图片/附件
                print(f"[_send_ai_reply_sync] 检查附件请求...")
                attachments_to_send = self._check_attachment_request(incoming_msg.content, final_knowledge)
                print(f"[_send_ai_reply_sync] 待发送附件: {len(attachments_to_send)}")
                
                # 生成AI回复 - 使用线程池执行异步函数
                import concurrent.futures
                llm = get_llm_service()
                
                def run_async():
                    """在新线程中运行异步函数"""
                    from database import SessionLocal
                    thread_db = SessionLocal()
                    try:
                        # 始终在新线程中创建新的事件循环，避免与主线程事件循环冲突
                        return asyncio.run(
                            llm.generate_reply(customer_data, list(reversed(messages_data)), final_knowledge, thread_db)
                        )
                    finally:
                        thread_db.close()
                
                # 在线程池中执行
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(run_async)
                    try:
                        reply = future.result(timeout=60)  # 增加到60秒超时
                    except concurrent.futures.TimeoutError:
                        logger.error(f"[AI回复] 60秒超时，发送默认回复")
                        default_reply = "老细，我地而家幫你計緊價，請稍等一陣，好快覆你！"
                        self._send_reply(customer_data['phone'], default_reply)
                        # 记录到数据库
                        with self._get_db() as db:
                            msg = Message(
                                customer_id=customer_data['id'],
                                content=default_reply,
                                direction="outgoing",
                                sender_name="AI助手"
                            )
                            db.add(msg)
                            db.commit()
                        # 标记原消息为已处理
                        if hasattr(incoming_msg, 'id') and incoming_msg.id:
                            self._mark_message_processed(incoming_msg.id)
                        return
                
                if reply:
                    # 解析 AI 回复中的附件发送标记 [SEND_ATTACHMENT:附件名称]
                    import re
                    attachment_markers = re.findall(r'\[SEND_ATTACHMENT:([^\]]+)\]', reply)
                    
                    # 移除标记后的干净回复
                    clean_reply = re.sub(r'\[SEND_ATTACHMENT:[^\]]+\]', '', reply).strip()
                    
                    # 发送干净的回复
                    self._send_reply(customer_data['phone'], clean_reply)
                    self._record_outgoing_message(customer_data['id'], clean_reply)
                    print(f"[AI回复] 已发送给客户 {customer_data['phone']}")
                    # 标记原消息为已处理
                    if hasattr(incoming_msg, 'id') and incoming_msg.id:
                        self._mark_message_processed(incoming_msg.id)
                    
                    # 如果 AI 指定了要发送的附件
                    if attachment_markers:
                        print(f"[_send_ai_reply_sync] AI 指定发送附件: {attachment_markers}")
                        import time
                        time.sleep(1)  # 等待文字消息发送完成
                        
                        for att_name in attachment_markers:
                            att_name = att_name.strip()
                            print(f"[_send_ai_reply_sync] 查找附件: {att_name}")
                            # 在所有知识库附件中查找匹配的附件
                            found = False
                            for att in all_kb_attachments:
                                # 按附件名称匹配
                                if att['name'] == att_name or att_name in att['name'] or att['name'] in att_name:
                                    print(f"[_send_ai_reply_sync] 找到附件: {att['name']}, 路径: {att['file_path']}")
                                    # 检查文件是否存在
                                    if os.path.exists(att['file_path']):
                                        file_type = 'image' if any(ext in att['file_path'].lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']) else 'document'
                                        att_to_send = {
                                            'name': att['name'],
                                            'path': att['file_path'],
                                            'type': file_type
                                        }
                                        self._send_attachment(customer_data, att_to_send)
                                        found = True
                                        time.sleep(0.5)
                                        break
                                    else:
                                        print(f"[_send_ai_reply_sync] 附件文件不存在: {att['file_path']}")
                            if not found:
                                print(f"[_send_ai_reply_sync] 未找到附件: {att_name}")
                    else:
                        print(f"[_send_ai_reply_sync] AI 未指定发送附件")
                else:
                    raise Exception("LLM 返回空回复")
                
                # 成功发送，跳出重试循环
                return
            
            except Exception as e:
                retry_count += 1
                if retry_count <= max_retries:
                    logger.warning(f"[AI回复] 生成失败，正在进行第 {retry_count} 次重试: {e}")
                    import time
                    time.sleep(1)  # 等待1秒后重试
                else:
                    logger.error(f"[AI回复] 生成失败，已重试 {max_retries} 次: {e}", exc_info=True)
                    # 使用默认模板回复
                    default_reply = self.AUTO_REPLY_TEMPLATES.get(
                        customer.category, 
                        self.AUTO_REPLY_TEMPLATES["new"]
                    )
                    self._send_reply(customer.phone, default_reply)
                    self._record_outgoing_message(customer.id, default_reply)
                    # 标记原消息为已处理（默认回复也算已处理）
                    if hasattr(incoming_msg, 'id') and incoming_msg.id:
                        self._mark_message_processed(incoming_msg.id)
    
    async def _async_send_ai_reply(self, customer_id: int, incoming_msg: Message, messages, knowledge):
        """异步发送AI回复"""
        from database import SessionLocal
        db = SessionLocal()
        max_retries = 1
        retry_count = 0
        
        while retry_count <= max_retries:
            try:
                # 重新查询客户（避免Session问题）
                customer = db.query(Customer).filter(Customer.id == customer_id).first()
                if not customer:
                    logger.warning(f"[AI回复] 客户不存在: {customer_id}")
                    return
                
                llm = get_llm_service()
                reply = await llm.generate_reply(customer, list(reversed(messages)), knowledge, db)
                
                if reply:
                    self._send_reply(customer.phone, reply)
                    self._record_outgoing_message(customer.id, reply)
                    print(f"[AI回复] 已发送给客户 {customer.phone}")
                
                # 成功发送，跳出重试循环
                return
            except Exception as e:
                retry_count += 1
                if retry_count <= max_retries:
                    logger.warning(f"[AI回复] 异步生成失败，正在进行第 {retry_count} 次重试: {e}")
                    await asyncio.sleep(1)  # 等待1秒后重试
                else:
                    logger.error(f"[AI回复] 异步生成失败，已重试 {max_retries} 次: {e}", exc_info=True)
            finally:
                if retry_count > max_retries:
                    db.close()
    
    async def _send_ai_reply(self, customer: Customer, incoming_msg: Message):
        """发送AI智能回复（异步版本 - 保留用于兼容）"""
        max_retries = 1
        retry_count = 0
        
        while retry_count <= max_retries:
            try:
                # 获取历史消息
                with self._get_db() as db:
                    messages = db.query(Message).filter(
                        Message.customer_id == customer.id
                    ).order_by(Message.created_at.desc()).limit(10).all()
                
                # 获取相关知识和附件
                kb = get_knowledge_base()
                knowledge = kb.get_relevant_knowledge(incoming_msg.content)
                
                # 检查客户是否在询问图片/附件
                attachments_to_send = self._check_attachment_request(incoming_msg.content, knowledge)
                
                # 生成AI回复（传入db以支持智能体选择）
                llm = get_llm_service()
                reply = await llm.generate_reply(customer, list(reversed(messages)), knowledge, self.db)
                
                # 发送消息
                if reply:
                    self._send_reply(customer.phone, reply)
                    self._record_outgoing_message(customer.id, reply)
                    print(f"[AI回复] 已发送给客户 {customer.phone}")
                    
                    # 如果有匹配的附件，延迟后发送附件
                    if attachments_to_send:
                        await asyncio.sleep(1)  # 等待文字消息发送完成
                        for att in attachments_to_send[:3]:  # 最多发送3个附件
                            self._send_attachment(customer, att)
                            await asyncio.sleep(0.5)
                    
                    # 应用自动打标签规则
                    self.apply_auto_tags(customer.id, incoming_msg.content)
                
                # 成功发送，跳出重试循环
                return
            except Exception as e:
                retry_count += 1
                if retry_count <= max_retries:
                    logger.warning(f"[AI回复] 生成失败，正在进行第 {retry_count} 次重试: {e}")
                    await asyncio.sleep(1)  # 等待1秒后重试
                else:
                    logger.error(f"[AI回复] 生成失败，已重试 {max_retries} 次: {e}")
                    # 使用默认模板回复
                    default_reply = self.AUTO_REPLY_TEMPLATES.get(
                        customer.category, 
                        self.AUTO_REPLY_TEMPLATES["new"]
                    )
                    self._send_reply(customer.phone, default_reply)
                    self._record_outgoing_message(customer.id, default_reply)
    
    def _check_attachment_request(self, message_content: str, knowledge: str) -> list:
        """检查客户是否在请求图片/附件，返回要发送的附件列表"""
        import re
        
        content_preview = message_content[:20] + "..." if len(message_content) > 20 else message_content
        print(f"[_check_attachment_request] 检查消息: {content_preview}")
        
        # 请求附件的关键词
        attachment_keywords = [
            '图片', '照片', '截图', '看看', '发给我', '发一下', '发来看看',
            '界面', '样子', '外观', '效果', '示例', '案例',
            'image', 'picture', 'photo', 'screenshot', 'look', 'show me'
        ]
        
        # 检查是否包含关键词
        message_lower = message_content.lower()
        is_requesting = any(kw in message_content or kw in message_lower for kw in attachment_keywords)
        
        print(f"[_check_attachment_request] 是否匹配关键词: {is_requesting}")
        
        if not is_requesting:
            return []
        
        # 从知识库中提取附件信息
        # 格式：[附件: 名称 | 路径]
        attachments = []
        pattern = r'\[附件:\s*([^|]+)\|\s*([^\]]+)\]'
        matches = re.findall(pattern, knowledge)
        
        print(f"[_check_attachment_request] 知识库中附件数量: {len(matches)}")
        
        for name, path in matches:
            path_stripped = path.strip()
            print(f"[_check_attachment_request] 检查路径: {path_stripped[:80]}...")
            # 检查路径是否存在
            if os.path.exists(path_stripped):
                file_type = 'image' if any(ext in path_stripped.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']) else 'document'
                attachments.append({
                    'name': name.strip(),
                    'path': path_stripped,
                    'type': file_type
                })
                print(f"[_check_attachment_request] 添加附件: {name.strip()}")
            else:
                print(f"[_check_attachment_request] 路径不存在")
        
        print(f"[_check_attachment_request] 返回附件数量: {len(attachments)}")
        return attachments
    
    def _extract_attachments_from_knowledge(self, knowledge: str) -> list:
        """从知识库内容中提取所有附件信息"""
        import re
        attachments = []
        pattern = r'\[附件:\s*([^|]+)\|\s*([^\]]+)\]'
        matches = re.findall(pattern, knowledge)
        
        for name, path in matches:
            path_stripped = path.strip()
            if os.path.exists(path_stripped):
                file_type = 'image' if any(ext in path_stripped.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']) else 'document'
                attachments.append({
                    'name': name.strip(),
                    'path': path_stripped,
                    'type': file_type
                })
        
        return attachments
    
    def _send_attachment(self, customer_data: dict, attachment: dict):
        """发送附件给客户"""
        try:
            file_path = attachment['path']
            file_type = attachment['type']
            phone = customer_data.get('phone') or customer_data.get('phone_number')
            customer_id = customer_data.get('id')
            
            # 验证路径安全性
            kb_files_dir = os.path.join(os.path.dirname(__file__), "data", "knowledge_files")
            abs_path = os.path.abspath(file_path)
            abs_kb_dir = os.path.abspath(kb_files_dir)
            
            if not abs_path.startswith(abs_kb_dir):
                logger.error(f"[附件] 路径越界: {file_path}")
                return
            
            print(f"[_send_attachment] 开始发送附件: {attachment['name']}, 类型: {file_type}")
            print(f"[_send_attachment] 文件路径: {file_path}")
            print(f"[_send_attachment] 客户电话: {phone}")
            
            if not phone:
                print(f"[_send_attachment] 错误: 客户电话为空")
                return
            
            if file_type == 'image':
                print(f"[_send_attachment] 调用 send_image...")
                success = self.wa_client.send_image(phone, file_path, caption=attachment['name'])
            else:
                file_name = os.path.basename(file_path)
                print(f"[_send_attachment] 调用 send_document...")
                success = self.wa_client.send_document(phone, file_path, file_name)
            
            print(f"[_send_attachment] 发送结果: {success}")
            
            if success:
                print(f"[AI回复] 已发送附件: {attachment['name']} 给客户 {phone}")
                # 记录到数据库
                if customer_id:
                    with self._get_db() as db:
                        message = Message(
                            customer_id=customer_id,
                            direction="outgoing",
                            content=f"[附件] {attachment['name']}",
                            sender_name="Bot",
                            is_read=True
                        )
                        db.add(message)
                        db.commit()
            else:
                logger.error(f"[通信服务] 发送附件失败: {attachment['name']}")
        except Exception as e:
            logger.error(f"[通信服务] 发送附件异常: {e}", exc_info=True)
    
    def _send_reply(self, phone: str, text: str) -> bool:
        """发送回复消息 - 使用正确的JID格式"""
        # 统一使用 WhatsApp 客户端的 send_message，它内部会处理 JID 格式
        # 不再自行拼接 JID
        if not phone:
            logger.warning("[通信服务] 无法发送回复：手机号为空")
            return False
        
        try:
            result = self.wa_client.send_message(phone, text)
            if not result:
                logger.error(f"[通信服务] 发送回复失败: {phone}")
            return result
        except Exception as e:
            logger.error(f"[通信服务] 发送回复异常: {e}", exc_info=True)
            return False
    
    def _record_outgoing_message(self, customer_id: int, content: str):
        """记录发出的消息"""
        with self._get_db() as db:
            message = Message(
                customer_id=customer_id,
                direction="outgoing",
                content=content,
                sender_name="Bot",
                is_read=True
            )
            db.add(message)
            db.commit()
    
    def apply_auto_tags(self, customer_id: int, message_content: str = None):
        """为客户应用自动打标签规则"""
        try:
            with self._get_db() as db:
                # 一次性统计消息数据（合并为单条 GROUP BY 查询）
                counts = db.query(
                    Message.direction,
                    func.count(Message.id).label('count')
                ).filter(
                    Message.customer_id == customer_id
                ).group_by(Message.direction).all()
                
                incoming_count = next((c.count for c in counts if c.direction == "incoming"), 0)
                outgoing_count = next((c.count for c in counts if c.direction == "outgoing"), 0)
                    
                has_two_way_chat = incoming_count >= 2 and outgoing_count >= 1
                    
                price_keywords = [
                    "报价", "价格", "多少錢", "多少錢", "费用", "价位",
                    "需求", "定制", "投标", "采购",
                    "cost", "price", "quote", "pricing", "budget", "how much", "rate"
                ]
                deal_keywords = [
                    "成交", "订单", "付款", "购买", "购入", "合同", "发货",
                    "起单", "订货", "签订", "定金", "考虑合作",
                    "order", "purchase", "deal", "confirmed", "payment", "invoice", "contract"
                ]
                    
                content_lower = message_content.lower() if message_content else ""
                has_price_inquiry = any(kw in content_lower for kw in price_keywords)
                has_deal_intent = any(kw in content_lower for kw in deal_keywords)
                    
                # 获取所有启用的自动标签规则
                rules = db.query(AutoTagRule).filter(
                    AutoTagRule.is_active == True
                ).order_by(AutoTagRule.priority.desc()).all()
                    
                applied_tags = []
                    
                for rule in rules:
                    should_apply = False
                        
                    if rule.condition_type == "new_customer":
                        should_apply = incoming_count <= 2 and not has_two_way_chat and not has_price_inquiry
                        
                    elif rule.condition_type == "follow_up" or rule.condition_type == "message_received":
                        should_apply = has_two_way_chat or has_price_inquiry
                        
                    elif rule.condition_type == "old_customer":
                        should_apply = (has_price_inquiry or has_two_way_chat) and has_deal_intent
                        
                    elif rule.condition_type == "quote_requested":
                        should_apply = has_price_inquiry
                        
                    elif rule.condition_type == "keyword_match":
                        # 关键词匹配
                        if message_content and rule.condition_config.get("keywords"):
                            keywords = rule.condition_config["keywords"]
                            should_apply = any(keyword in content_lower for keyword in keywords)
                    
                    if should_apply:
                        # 检查是否已经有这个标签
                        existing = db.query(CustomerTagAssociation).filter(
                            CustomerTagAssociation.customer_id == customer_id,
                            CustomerTagAssociation.tag_id == rule.tag_id
                        ).first()
                        
                        if not existing:
                            # 添加标签
                            association = CustomerTagAssociation(customer_id=customer_id, tag_id=rule.tag_id)
                            db.add(association)
                            
                            # 记录日志
                            log = CustomerTagLog(
                                customer_id=customer_id,
                                tag_id=rule.tag_id,
                                action="add",
                                source="auto_rule",
                                source_id=rule.id
                            )
                            db.add(log)
                            
                            applied_tags.append({
                                "tag_id": rule.tag_id,
                                "tag_name": rule.tag.name if rule.tag else None,
                                "rule_name": rule.name
                            })
                
                if applied_tags:
                    db.commit()
                    print(f"[自动标签] 为客户 {customer_id} 应用了 {len(applied_tags)} 个标签: {[t['tag_name'] for t in applied_tags]}")
                
                return applied_tags
            
        except Exception as e:
            logger.error(f"[自动标签] 应用失败: {e}", exc_info=True)
            return []
    
    def execute_communication_plan(self, plan_id: int, customer_id: int) -> bool:
        """执行沟通计划"""
        with self._get_db() as db:
            plan = db.query(CommunicationPlan).filter(
                CommunicationPlan.id == plan_id
            ).first()
            
            if not plan or not plan.is_active:
                return False
            
            customer = db.query(Customer).filter(
                Customer.id == customer_id
            ).first()
            
            if not customer:
                return False
            
            # 发送计划消息
            success = self._send_reply(customer.phone, plan.message_template)
            
            # 记录执行
            execution = PlanExecution(
                plan_id=plan_id,
                customer_id=customer_id,
                status="sent" if success else "failed",
                executed_at=datetime.utcnow() if success else None
            )
            db.add(execution)
            db.commit()
            
            return success
    
    def schedule_plan_execution(self, plan_id: int, customer_id: int, 
                                delay_minutes: int = 0):
        """计划执行沟通计划"""
        with self._get_db() as db:
            execution = PlanExecution(
                plan_id=plan_id,
                customer_id=customer_id,
                status="pending",
                scheduled_at=datetime.utcnow() + timedelta(minutes=delay_minutes)
            )
            db.add(execution)
            db.commit()
    
    def process_pending_plans(self):
        """处理待执行的沟通计划"""
        with self._get_db() as db:
            pending = db.query(PlanExecution).filter(
                PlanExecution.status == "pending",
                PlanExecution.scheduled_at <= datetime.utcnow()
            ).all()
            
            for execution in pending:
                success = self.execute_communication_plan(
                    execution.plan_id, 
                    execution.customer_id
                )
                # 重新查询execution以确保对象附加到当前Session
                db_exec = db.query(PlanExecution).filter(
                    PlanExecution.id == execution.id
                ).first()
                if db_exec:
                    if success:
                        db_exec.status = "sent"
                        db_exec.executed_at = datetime.utcnow()
                    else:
                        db_exec.status = "failed"
                        db_exec.error_message = "发送失败"
                    
                    db.commit()


class NotificationService:
    """通知服务 - 处理人工通知"""
    
    def __init__(self, db: Optional[Session], whatsapp_client):
        self.db = db
        self.wa_client = whatsapp_client
    
    @contextmanager
    def _get_db(self):
        """获取数据库会话（上下文管理器模式）"""
        db = self.db if self.db is not None else SessionLocal()
        try:
            yield db
        finally:
            if self.db is None:
                db.close()
    
    def notify_agents_new_message(self, customer: Customer, 
                                   message: Message,
                                   conversation: Conversation):
        """通知所有在线客服有新消息"""
        # 获取在线客服
        with self._get_db() as db:
            online_agents = db.query(Agent).filter(
                Agent.is_online == True,
                Agent.is_active == True
            ).all()
            
            for agent in online_agents:
                # 发送 WhatsApp 通知给客服
                if agent.phone:
                    self._send_agent_notification(agent.phone, customer, message)
    
    def _send_agent_notification(self, agent_phone: str, 
                                  customer: Customer, 
                                  message: Message):
        """发送通知给客服"""
        # 清理电话号码，直接传递给 send_message 处理 JID 格式
        phone = agent_phone.replace("+", "").replace(" ", "")
        
        notification = f"""🔔 新客户咨询

客户: {customer.name or customer.phone}
分类: {self._get_category_name(customer.category)}
消息: {message.content[:50]}...

点击链接接手:
http://localhost:3000/chat/{customer.id}
        """
        
        self.wa_client.send_message(phone, notification)
    
    def _get_category_name(self, category: str) -> str:
        """获取分类名称"""
        names = {
            "new": "新客户",
            "lead": "意向客户",
            "returning": "老客户"
        }
        return names.get(category, category)
    
    def handover_conversation(self, conversation_id: int, 
                               agent_id: int) -> bool:
        """客服接手会话"""
        with self._get_db() as db:
            conversation = db.query(Conversation).filter(
                Conversation.id == conversation_id
            ).first()
            
            if not conversation:
                return False
            
            conversation.status = "handover"
            conversation.assigned_agent_id = agent_id
            db.commit()
            
            # 发送通知给客户
            customer = db.query(Customer).filter(
                Customer.id == conversation.customer_id
            ).first()
            
            if customer:
                agent = db.query(Agent).filter(
                    Agent.id == agent_id
                ).first()
                
                handover_text = f"您好！我是专属客服 {agent.name if agent else '小芃'}，很高兴为您服务！"
                
                # 直接传递手机号，让 send_message 内部处理 JID 格式
                self.wa_client.send_message(customer.phone, handover_text)
            
            return True
