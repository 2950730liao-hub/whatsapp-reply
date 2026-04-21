"""
WhatsApp 智能客户系统 - FastAPI 主应用
"""
import os
import asyncio
import threading
import logging
from contextlib import asynccontextmanager
from typing import List, Optional, Dict
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from database import init_db, get_db, SessionLocal, Customer, Message, Conversation, Agent, CommunicationPlan, AIAgent, CustomerTag, CustomerTagAssociation, AgentTagBinding, LLMProvider, LLMModel, AutoTagRule, CustomerTagLog

# 尝试使用 Neonize，否则回退到 CLI
try:
    from neonize_client import NeonizeWhatsAppClient, MessageSyncer, init_whatsapp_client, set_client_ready_event
    print("[Main] 使用 Neonize WhatsApp 客户端")
    USE_NEONIZE = True
except ImportError as e:
    print(f"[Main] Neonize 不可用 ({e})，使用 CLI 客户端")
    from whatsapp_client import WhatsAppClient, MessageSyncer
    USE_NEONIZE = False
    
    def init_whatsapp_client():
        return WhatsAppClient()
    
    def set_client_ready_event(event):
        pass  # CLI 模式下不需要

from communication_service import CommunicationService, NotificationService
from llm_service import get_llm_service
from knowledge_base import get_knowledge_base
from quotation_service import get_quotation_service
from memo_service import get_memo_service
from config_service import get_config_service
from scheduler_service import get_scheduler_service
from schedule_runner import get_schedule_runner


# 全局状态
whatsapp_client = None
message_syncer = None
active_websockets: set = set()

# 并发安全锁
_ws_lock = threading.Lock()
_client_lock = threading.Lock()
_client_ready = threading.Event()


class CustomerTagInfo(BaseModel):
    id: int
    name: str
    color: str

class CustomerResponse(BaseModel):
    id: int
    phone: str
    name: Optional[str]
    category: str
    status: str
    created_at: datetime
    tags: List[CustomerTagInfo] = []
    
    class Config:
        from_attributes = True


class MessageResponse(BaseModel):
    id: int
    customer_id: int
    content: str
    direction: str
    sender_name: str
    is_read: bool
    created_at: datetime
    
    class Config:
        from_attributes = True


class ConversationResponse(BaseModel):
    id: int
    customer_id: int
    customer_name: Optional[str]
    customer_phone: str
    status: str
    assigned_agent_id: Optional[int]
    last_message_at: Optional[datetime]
    created_at: datetime
    
    class Config:
        from_attributes = True


class SendMessageRequest(BaseModel):
    content: str


class HandoverRequest(BaseModel):
    agent_id: int


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global whatsapp_client, message_syncer
    
    # 启动时初始化
    print("🚀 启动 WhatsApp 智能客户系统...")
    
    # 初始化数据库
    init_db()
    print("✅ 数据库初始化完成")
    
    # 初始化 WhatsApp 客户端
    with _client_lock:
        whatsapp_client = init_whatsapp_client()
    
    if USE_NEONIZE:
        # Neonize 方案：启动客户端（会显示二维码）
        print("🚀 启动 Neonize WhatsApp 客户端...")
        
        # 设置客户端就绪事件回调
        set_client_ready_event(_client_ready)
        
        client_thread = threading.Thread(
            target=whatsapp_client.connect_with_retry, 
            daemon=True  # 保持 daemon=True
        )
        client_thread.start()
        print("✅ Neonize 客户端已在后台启动")
        print("⚠️  首次使用请扫描二维码登录 WhatsApp")
        
        # 等待客户端初始化完成
        _client_ready.wait(timeout=5)
        print("✅ 客户端初始化完成信号已接收")
        
        # 注册消息处理器
        from communication_service import CommunicationService
        comm_service = CommunicationService(None, whatsapp_client)  # db 会在处理时获取
        whatsapp_client.on_message(comm_service.handle_incoming_message)
        
        print("✅ 消息处理器已注册")
    else:
        # CLI 方案
        if not whatsapp_client.is_authenticated():
            print("⚠️  WhatsApp 未登录，请运行: whatsapp auth login")
        else:
            print("✅ WhatsApp 已连接")
            
            # 启动 sync --follow 后台进程保持连接
            import subprocess
            subprocess.Popen(
                ["python3", "whatsapp_sync_manager.py"],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                stdout=open("/tmp/sync_manager.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
            print("✅ WhatsApp Sync 管理器已启动")
            
            # 启动消息同步
            message_syncer = MessageSyncer(whatsapp_client)
            asyncio.create_task(message_syncer.start_polling(interval=1))
            print("✅ 消息同步已启动")
        
        # 启动定时发送执行器
        runner = get_schedule_runner(whatsapp_client)
        runner.start()
        print("✅ 定时发送执行器已启动")
    
    yield
    
    # 关闭时清理
    print("🛑 关闭系统...")
    
    # 1. 停止 WhatsApp 客户端
    with _client_lock:
        if whatsapp_client:
            try:
                whatsapp_client.disconnect()
                logger.info("WhatsApp 客户端已断开")
            except Exception as e:
                logger.error(f"断开 WhatsApp 客户端失败: {e}")
    
    # 2. 停止消息同步
    with _client_lock:
        if message_syncer:
            try:
                message_syncer.stop()
            except Exception as e:
                logger.error(f"停止消息同步失败: {e}")
    
    # 3. 停止定时任务
    runner = get_schedule_runner()
    runner.stop()
    
    # 4. 关闭 WebSocket 连接
    with _ws_lock:
        ws_copy = active_websockets.copy()
        active_websockets.clear()
    for ws in ws_copy:
        try:
            await ws.close()
        except Exception:
            pass
    
    print("✅ 系统已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="WhatsApp 智能客户系统",
    description="基于 WhatsApp CLI 的客户关系管理系统",
    version="1.0.0",
    lifespan=lifespan
)

# 挂载静态文件
import os
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def serve_index():
    """服务首页"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "WhatsApp CRM API is running"}

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应限制域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局异常处理中间件
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "error": str(exc)}
    )


# ============ WebSocket 实时通信 ============

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 连接 - 实时推送新消息"""
    await websocket.accept()
    with _ws_lock:
        active_websockets.add(websocket)
    
    try:
        while True:
            # 接收客户端心跳
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        with _ws_lock:
            active_websockets.discard(websocket)


async def broadcast_new_message(message: dict):
    """广播新消息给所有连接的客户端"""
    disconnected = []
    with _ws_lock:
        ws_copy = active_websockets.copy()
    for ws in ws_copy:
        try:
            await ws.send_json({
                "type": "new_message",
                "data": message
            })
        except Exception as e:
            logger.warning(f"Failed to send message to websocket client: {e}")
            disconnected.append(ws)
    
    # 清理断开的连接
    with _ws_lock:
        for ws in disconnected:
            active_websockets.discard(ws)


# ============ 认证相关 API ============

@app.get("/api/auth/status")
async def get_auth_status():
    """获取 WhatsApp 登录状态"""
    global USE_NEONIZE
    
    if not whatsapp_client:
        return {
            "connected": False, 
            "error": "客户端未初始化",
            "backend": "neonize" if USE_NEONIZE else "cli"
        }
    
    try:
        if USE_NEONIZE:
            # Neonize 方案
            from neonize_client import get_current_qr
            qr_code = get_current_qr()
            
            return {
                "connected": whatsapp_client.connected,
                "logged_in": whatsapp_client.connected,
                "backend": "neonize",
                "qr_code": qr_code,  # 返回二维码数据
                "message": "请扫描二维码登录" if not whatsapp_client.connected else "已连接"
            }
        else:
            # CLI 方案
            status = whatsapp_client.auth_status()
            return {
                "connected": status.get("connected", False),
                "logged_in": status.get("logged_in", False),
                "database": status.get("database", {}),
                "backend": "cli"
            }
    except Exception as e:
        return {
            "connected": False, 
            "error": str(e),
            "backend": "neonize" if USE_NEONIZE else "cli"
        }


@app.post("/api/auth/refresh-qr")
async def refresh_qr_code():
    """刷新二维码 - 清除当前二维码等待新生成"""
    global USE_NEONIZE
    
    if not USE_NEONIZE:
        return {"success": False, "message": "CLI 模式不支持刷新二维码"}
    
    try:
        from neonize_client import clear_current_qr, get_current_qr
        clear_current_qr()
        
        # 等待新二维码生成（最多等待 10 秒）
        for i in range(10):
            await asyncio.sleep(1)
            new_qr = get_current_qr()
            if new_qr:
                return {
                    "success": True,
                    "qr_code": new_qr,
                    "message": "新二维码已生成"
                }
        
        return {
            "success": False,
            "message": "等待新二维码超时，请刷新页面"
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"刷新失败: {str(e)}"
        }


# 导入 QR 码捕获模块
from qr_terminal import get_qr_capture, TerminalQRCapture

# 全局 QR 码捕获器
qr_capture: TerminalQRCapture = get_qr_capture()

@app.post("/api/auth/qr")
async def get_qr_code():
    """获取 QR 码进行登录 - 启动 whatsapp auth login 并捕获 QR 码"""
    try:
        # 检查是否已登录
        if whatsapp_client and whatsapp_client.is_authenticated():
            return {"success": False, "message": "已经登录了"}
        
        # 检查是否已经在登录中
        if qr_capture.is_running():
            return {
                "success": True,
                "message": "登录进程已在运行，请查看 QR 码",
                "status": "pending"
            }
        
        # 启动登录并捕获 QR 码
        qr_data_store = {"qr_image": None, "captured": False}
        
        def on_qr_captured(qr_image: str):
            qr_data_store["qr_image"] = qr_image
            qr_data_store["captured"] = True
            print("✅ QR 码已捕获并转换")
        
        def on_login_success():
            print("✅ 登录成功")
            # 登录成功后自动同步联系人
            asyncio.create_task(auto_sync_contacts_after_login())
        
        async def auto_sync_contacts_after_login():
            """登录成功后自动同步联系人"""
            try:
                # 等待几秒钟确保连接稳定
                await asyncio.sleep(3)
                
                # 获取当前登录用户
                if whatsapp_client:
                    current_user = whatsapp_client.get_current_user()
                    print(f"[登录后同步] 当前用户: {current_user}")
                    
                    # 同步联系人
                    from sqlalchemy.orm import Session
                    db = SessionLocal()
                    try:
                        # 调用同步联系人逻辑
                        contacts = whatsapp_client.get_contacts()
                        chats = whatsapp_client.get_chats()
                        
                        new_count = 0
                        for contact in contacts:
                            phone = contact.get("phone_number", "")
                            name = contact.get("name", "")
                            if phone:
                                existing = db.query(Customer).filter(Customer.phone == phone).first()
                                if not existing:
                                    customer = Customer(phone=phone, name=name or phone, category="new", status="active")
                                    db.add(customer)
                                    new_count += 1
                        
                        for chat in chats:
                            jid = chat.get("jid", "")
                            name = chat.get("name", "")
                            if "@" in jid:
                                phone = jid.split("@")[0]
                                existing = db.query(Customer).filter(Customer.phone == phone).first()
                                if not existing:
                                    customer = Customer(phone=phone, name=name or phone, category="new", status="active")
                                    db.add(customer)
                                    new_count += 1
                        
                        db.commit()
                        print(f"[登录后同步] 新增 {new_count} 个客户")
                        
                        # 重新启动消息同步
                        global message_syncer
                        with _client_lock:
                            if message_syncer:
                                message_syncer.stop()
                            message_syncer = MessageSyncer(whatsapp_client)
                        asyncio.create_task(message_syncer.start_polling(interval=1))
                        print("✅ 消息同步已重新启动")
                        
                    finally:
                        db.close()
                        
            except Exception as e:
                print(f"[登录后同步] 失败: {e}")
        
        success = qr_capture.start_login(
            on_qr_captured=on_qr_captured,
            on_login_success=on_login_success
        )
        
        if not success:
            return {"success": False, "message": "启动登录失败"}
        
        # 等待 QR 码捕获（最多 15 秒）
        import asyncio
        for i in range(30):  # 30 * 0.5 = 15 秒
            await asyncio.sleep(0.5)
            if qr_data_store["captured"] and qr_data_store["qr_image"]:
                return {
                    "success": True,
                    "qr_image": qr_data_store["qr_image"],
                    "message": "请使用 WhatsApp 扫描此 QR 码"
                }
            # 每 3 秒输出一次等待信息
            if i > 0 and i % 6 == 0:
                print(f"[QR Capture] 等待中... {i//2}秒")
        
        # 如果还没有捕获到 QR 码，返回等待状态
        return {
            "success": True,
            "message": "正在获取 QR 码，请稍候...",
            "status": "pending",
            "has_qr": qr_capture.has_qr if hasattr(qr_capture, 'has_qr') else False
        }
        
    except Exception as e:
        return {"success": False, "message": f"启动登录失败: {str(e)}"}


@app.get("/api/auth/qr/status")
async def get_qr_status():
    """获取 QR 码状态"""
    has_qr = hasattr(qr_capture, 'has_qr') and qr_capture.has_qr
    qr_image = getattr(qr_capture, 'qr_image', None)
    
    return {
        "is_running": qr_capture.is_running(),
        "has_qr": has_qr,
        "qr_image": qr_image  # 如果已生成，返回图片
    }


@app.post("/api/auth/qr/cancel")
async def cancel_login():
    """取消登录进程"""
    qr_capture.stop()
    return {"success": True, "message": "已取消登录"}


@app.post("/api/auth/logout")
async def logout():
    """退出登录 - 清理当前会话并停止同步"""
    global message_syncer
    try:
        # 停止消息同步
        with _client_lock:
            if message_syncer:
                message_syncer.stop()
                message_syncer = None
                print("✅ 消息同步已停止")
        
        # 执行退出登录
        with _client_lock:
            if whatsapp_client:
                whatsapp_client.logout()
                print("✅ WhatsApp 已退出登录")
        
        return {"success": True, "message": "已退出登录并清理会话"}
    except Exception as e:
        return {"success": False, "message": str(e)}


class BatchImportCustomer(BaseModel):
    """批量导入客户请求"""
    customers: List[Dict[str, str]]  # [{"phone": "123456789", "name": "张三"}, ...]

@app.post("/api/customers/batch-import")
async def batch_import_customers(
    data: BatchImportCustomer,
    db: Session = Depends(get_db)
):
    """批量导入客户 - 从 WPS/Excel/CSV 导入"""
    try:
        imported = []
        skipped = []
        
        for item in data.customers:
            phone = item.get("phone", "").strip()
            name = item.get("name", "").strip()
            
            # 验证手机号
            if not phone:
                skipped.append({"phone": phone, "reason": "手机号为空"})
                continue
            
            # 清理手机号（只保留数字）
            phone = ''.join(c for c in phone if c.isdigit())
            
            if not phone:
                skipped.append({"phone": item.get("phone", ""), "reason": "手机号格式错误"})
                continue
            
            # 检查是否已存在
            existing = db.query(Customer).filter(Customer.phone == phone).first()
            if existing:
                skipped.append({"phone": phone, "reason": "客户已存在"})
                continue
            
            # 创建新客户
            customer = Customer(
                phone=phone,
                name=name or phone,
                category="new",
                status="active"
            )
            db.add(customer)
            imported.append({"phone": phone, "name": name or phone})
        
        db.commit()
        
        return {
            "success": True,
            "imported_count": len(imported),
            "skipped_count": len(skipped),
            "imported": imported,
            "skipped": skipped
        }
    
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"导入失败: {str(e)}")


@app.post("/api/auth/sync-contacts")
async def sync_contacts(db: Session = Depends(get_db)):
    """同步联系人列表 - 检查新客户"""
    try:
        if not whatsapp_client or not whatsapp_client.is_authenticated():
            return {"success": False, "message": "WhatsApp 未登录"}
        
        # 获取当前登录用户
        current_user = whatsapp_client.get_current_user()
        print(f"[联系人同步] 当前登录用户: {current_user}")
        
        # 获取联系人列表
        contacts = whatsapp_client.get_contacts()
        print(f"[联系人同步] 获取到 {len(contacts)} 个联系人")
        
        # 获取聊天列表（包含更多活跃客户）
        chats = whatsapp_client.get_chats()
        print(f"[联系人同步] 获取到 {len(chats)} 个聊天")
        
        new_customers = []
        updated_customers = []
        
        # 处理联系人
        for contact in contacts:
            phone = contact.get("phone_number", "")
            name = contact.get("name", "")
            jid = contact.get("jid", "")
            
            if not phone:
                continue
            
            # 检查客户是否已存在
            existing = db.query(Customer).filter(Customer.phone == phone).first()
            
            if not existing:
                # 创建新客户
                customer = Customer(
                    phone=phone,
                    name=name or phone,
                    category="new",
                    status="active"
                )
                db.add(customer)
                new_customers.append({"phone": phone, "name": name})
                print(f"[联系人同步] 新客户: {phone} ({name})")
            elif name and existing.name != name:
                # 更新客户名称
                existing.name = name
                updated_customers.append({"phone": phone, "name": name})
        
        # 处理聊天列表中的客户（可能不在联系人中）
        for chat in chats:
            jid = chat.get("jid", "")
            name = chat.get("name", "")
            
            # 从JID提取手机号
            if "@" in jid:
                phone = jid.split("@")[0]
            else:
                continue
            
            # 检查客户是否已存在
            existing = db.query(Customer).filter(Customer.phone == phone).first()
            
            if not existing:
                # 创建新客户
                customer = Customer(
                    phone=phone,
                    name=name or phone,
                    category="new",
                    status="active"
                )
                db.add(customer)
                new_customers.append({"phone": phone, "name": name})
                print(f"[聊天同步] 新客户: {phone} ({name})")
        
        db.commit()
        
        return {
            "success": True,
            "message": f"同步完成，新增 {len(new_customers)} 个客户，更新 {len(updated_customers)} 个客户",
            "new_customers": new_customers,
            "updated_customers": updated_customers,
            "total_contacts": len(contacts),
            "total_chats": len(chats),
            "current_user": current_user
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "message": f"同步失败: {str(e)}"}


# ============ API 路由 ============

@app.get("/api")
async def api_root():
    """API 根路径"""
    return {
        "name": "WhatsApp 智能客户系统 API",
        "version": "1.0.0",
        "status": "running"
    }


@app.get("/api/status")
async def get_status():
    """获取系统状态"""
    with _client_lock:
        client_connected = whatsapp_client.is_authenticated() if whatsapp_client else False
    with _ws_lock:
        ws_count = len(active_websockets)
    return {
        "whatsapp_connected": client_connected,
        "sync_running": message_syncer.is_running if message_syncer else False,
        "websocket_clients": ws_count
    }


# ============ 客户管理 API ============

@app.get("/api/customers")
async def get_customers(
    category: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """获取客户列表"""
    try:
        query = db.query(Customer)
        
        if category:
            query = query.filter(Customer.category == category)
        if status:
            query = query.filter(Customer.status == status)
        
        customers = query.order_by(Customer.updated_at.desc()).all()
        
        # 构建响应，包含标签信息
        result = []
        for customer in customers:
            tags = []
            try:
                # 安全地获取标签
                if hasattr(customer, 'tags') and customer.tags:
                    for tag_assoc in customer.tags:
                        try:
                            if tag_assoc and tag_assoc.tag and tag_assoc.tag.is_active:
                                tags.append({
                                    "id": tag_assoc.tag.id,
                                    "name": tag_assoc.tag.name,
                                    "color": tag_assoc.tag.color
                                })
                        except Exception:
                            continue
            except Exception:
                pass  # 如果获取标签失败，继续处理客户
            
            customer_data = {
                "id": customer.id,
                "phone": customer.phone,
                "name": customer.name,
                "category": customer.category,
                "status": customer.status,
                "created_at": customer.created_at.isoformat() if customer.created_at else None,
                "tags": tags
            }
            result.append(customer_data)
        
        return result
    except Exception as e:
        print(f"Error in get_customers: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"获取客户列表失败: {str(e)}")


@app.get("/api/customers/{customer_id}", response_model=CustomerResponse)
async def get_customer(customer_id: int, db: Session = Depends(get_db)):
    """获取客户详情"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    return customer


@app.put("/api/customers/{customer_id}/category")
async def update_customer_category(
    customer_id: int,
    category: str,
    db: Session = Depends(get_db)
):
    """更新客户分类"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    
    customer.category = category
    db.commit()
    
    return {"success": True, "message": f"客户分类已更新为: {category}"}


# ============ 消息 API ============

@app.get("/api/customers/{customer_id}/messages", response_model=List[MessageResponse])
async def get_messages(customer_id: int, limit: int = 50, db: Session = Depends(get_db)):
    """获取客户消息历史"""
    messages = db.query(Message).filter(
        Message.customer_id == customer_id
    ).order_by(Message.created_at.desc()).limit(limit).all()
    
    # 标记为已读
    for msg in messages:
        if msg.direction == "incoming" and not msg.is_read:
            msg.is_read = True
    db.commit()
    
    return list(reversed(messages))  # 按时间正序返回


class ReceiveMessageRequest(BaseModel):
    content: str
    sender_name: str = "客户"


@app.post("/api/customers/{customer_id}/messages/receive")
async def receive_message(
    customer_id: int,
    request: ReceiveMessageRequest,
    db: Session = Depends(get_db)
):
    """接收客户发送的消息（用于测试自动回复）"""
    try:
        customer = db.query(Customer).filter(Customer.id == customer_id).first()
        if not customer:
            raise HTTPException(status_code=404, detail="客户不存在")
        
        # 创建 incoming 消息
        message = Message(
            customer_id=customer_id,
            direction="incoming",
            content=request.content,
            sender_name=request.sender_name,
            is_read=False
        )
        db.add(message)
        db.commit()
        db.refresh(message)
        
        # 触发自动回复
        try:
            from communication_service import CommunicationService
            comm_service = CommunicationService(db, whatsapp_client)
            need_notify = comm_service.handle_incoming_message(message, customer)
            
            return {
                "success": True, 
                "message": "消息已接收",
                "auto_reply_triggered": True,
                "need_notify": need_notify
            }
        except Exception as e:
            logger.error(f"自动回复触发失败: {e}", exc_info=True)
            return {
                "success": True,
                "message": "消息已接收",
                "auto_reply_triggered": False,
                "error": str(e)
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"接收消息失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"接收消息失败: {str(e)}")


@app.post("/api/customers/{customer_id}/messages")
async def send_message(
    customer_id: int,
    request: SendMessageRequest,
    db: Session = Depends(get_db)
):
    """发送消息给客户"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    
    if not whatsapp_client:
        raise HTTPException(status_code=503, detail="WhatsApp 客户端未就绪")
    
    # 发送消息 - 直接传递手机号，让 send_message 内部处理 JID 格式
    success = whatsapp_client.send_message(customer.phone, request.content)
    
    if success:
        # 记录到数据库
        message = Message(
            customer_id=customer_id,
            direction="outgoing",
            content=request.content,
            sender_name="Agent",
            is_read=True
        )
        db.add(message)
        db.commit()
        
        return {"success": True, "message": "消息已发送"}
    else:
        raise HTTPException(status_code=500, detail="消息发送失败")


# ============ 会话管理 API ============

@app.get("/api/conversations", response_model=List[ConversationResponse])
async def get_conversations(
    status: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """获取会话列表"""
    query = db.query(Conversation, Customer).join(Customer)
    
    if status:
        query = query.filter(Conversation.status == status)
    
    results = query.order_by(Conversation.last_message_at.desc()).all()
    
    conversations = []
    for conv, customer in results:
        conversations.append({
            "id": conv.id,
            "customer_id": conv.customer_id,
            "customer_name": customer.name,
            "customer_phone": customer.phone,
            "status": conv.status,
            "assigned_agent_id": conv.assigned_agent_id,
            "last_message_at": conv.last_message_at,
            "created_at": conv.created_at
        })
    
    return conversations


@app.post("/api/conversations/{conversation_id}/handover")
async def handover_conversation(
    conversation_id: int,
    request: HandoverRequest,
    db: Session = Depends(get_db)
):
    """客服接手会话"""
    notification_service = NotificationService(db, whatsapp_client)
    
    success = notification_service.handover_conversation(
        conversation_id, 
        request.agent_id
    )
    
    if success:
        return {"success": True, "message": "会话已接手"}
    else:
        raise HTTPException(status_code=404, detail="会话不存在")


@app.post("/api/conversations/{conversation_id}/close")
async def close_conversation(conversation_id: int, db: Session = Depends(get_db)):
    """关闭会话"""
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).first()
    
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    conversation.status = "closed"
    db.commit()
    
    return {"success": True, "message": "会话已关闭"}


# ============ 沟通计划 API ============

@app.get("/api/plans")
async def get_plans(db: Session = Depends(get_db)):
    """获取沟通计划列表"""
    plans = db.query(CommunicationPlan).all()
    return plans


@app.post("/api/plans/{plan_id}/execute/{customer_id}")
async def execute_plan(plan_id: int, customer_id: int, db: Session = Depends(get_db)):
    """手动执行沟通计划"""
    comm_service = CommunicationService(db, whatsapp_client)
    
    success = comm_service.execute_communication_plan(plan_id, customer_id)
    
    if success:
        return {"success": True, "message": "计划已执行"}
    else:
        raise HTTPException(status_code=500, detail="计划执行失败")


# ============ 大模型自动回复 API ============

@app.post("/api/customers/{customer_id}/ai-reply")
async def generate_ai_reply(customer_id: int, db: Session = Depends(get_db)):
    """生成AI自动回复"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    
    # 获取历史消息
    messages = db.query(Message).filter(
        Message.customer_id == customer_id
    ).order_by(Message.created_at.desc()).limit(10).all()
    
    # 获取相关知识
    kb = get_knowledge_base()
    knowledge = ""
    if messages:
        knowledge = kb.get_relevant_knowledge(messages[0].content)
    
    # 生成回复
    llm = get_llm_service()
    reply = await llm.generate_reply(customer, list(reversed(messages)), knowledge, db)
    
    return {"success": True, "reply": reply}


@app.post("/api/customers/{customer_id}/messages/ai-send")
async def send_ai_reply(customer_id: int, db: Session = Depends(get_db)):
    """生成并发送AI回复"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    
    # 获取历史消息
    messages = db.query(Message).filter(
        Message.customer_id == customer_id
    ).order_by(Message.created_at.desc()).limit(10).all()
    
    # 获取相关知识
    kb = get_knowledge_base()
    knowledge = ""
    if messages:
        knowledge = kb.get_relevant_knowledge(messages[0].content)
    
    # 生成回复
    llm = get_llm_service()
    reply = await llm.generate_reply(customer, list(reversed(messages)), knowledge, db)
    
    # 发送消息
    if not whatsapp_client:
        raise HTTPException(status_code=503, detail="WhatsApp 客户端未就绪")
    
    # 直接传递手机号，让 send_message 内部处理 JID 格式
    success = whatsapp_client.send_message(customer.phone, reply)
    
    if success:
        # 记录到数据库
        message = Message(
            customer_id=customer_id,
            direction="outgoing",
            content=reply,
            sender_name="AI助手",
            is_read=True
        )
        db.add(message)
        db.commit()
        
        return {"success": True, "message": "AI回复已发送", "content": reply}
    else:
        raise HTTPException(status_code=500, detail="发送失败")


# ============ 知识库 API ============

class DocumentRequest(BaseModel):
    title: str
    content: str
    category: str = "general"


@app.get("/api/knowledge/documents")
async def get_documents():
    """获取知识库文档列表"""
    kb = get_knowledge_base()
    documents = kb.get_all_documents()
    return {"success": True, "documents": documents}


@app.post("/api/knowledge/documents")
async def add_document(request: DocumentRequest):
    """添加知识库文档"""
    kb = get_knowledge_base()
    doc_id = kb.add_document(request.title, request.content, category=request.category)
    
    if doc_id:
        return {"success": True, "message": "文档已添加", "id": doc_id}
    else:
        raise HTTPException(status_code=500, detail="添加文档失败")


@app.delete("/api/knowledge/documents/{doc_id}")
async def delete_document(doc_id: int):
    """删除知识库文档"""
    kb = get_knowledge_base()
    success = kb.delete_document(doc_id)
    
    if success:
        return {"success": True, "message": "文档已删除"}
    else:
        raise HTTPException(status_code=500, detail="删除文档失败")


@app.get("/api/knowledge/search")
async def search_knowledge(q: str):
    """搜索知识库"""
    kb = get_knowledge_base()
    results = kb.search_documents(q)
    return {"success": True, "results": results}


# ============ 通知 API ============

@app.post("/api/notifications/subscribe")
async def subscribe_notification(subscription: dict):
    """订阅浏览器推送通知"""
    # 存储订阅信息到数据库或内存
    return {"success": True, "message": "订阅成功"}


@app.post("/api/notifications/test")
async def test_notification():
    """测试推送通知"""
    return {"success": True, "message": "测试通知已发送"}


# ============ 报价 API ============

@app.get("/api/materials")
async def get_materials(category: Optional[str] = None):
    """获取材料列表"""
    service = get_quotation_service()
    materials = service.get_materials(category)
    return {"success": True, "materials": materials}


@app.post("/api/quotations")
async def create_quotation(request: dict):
    """创建报价单"""
    service = get_quotation_service()
    result = service.create_quotation(
        customer_id=request.get("customer_id"),
        title=request.get("title"),
        items=request.get("items", []),
        notes=request.get("notes", "")
    )
    return {"success": True, "quotation": result}


@app.get("/api/customers/{customer_id}/quotations")
async def get_customer_quotations(customer_id: int):
    """获取客户报价单"""
    service = get_quotation_service()
    quotations = service.get_quotations(customer_id)
    return {"success": True, "quotations": quotations}


@app.get("/api/quotations/{quotation_id}")
async def get_quotation_detail(quotation_id: int):
    """获取报价单详情"""
    service = get_quotation_service()
    quotation = service.get_quotation(quotation_id)
    if quotation:
        return {"success": True, "quotation": quotation}
    raise HTTPException(status_code=404, detail="报价单不存在")


@app.post("/api/quotations/{quotation_id}/send")
async def send_quotation(quotation_id: int, db: Session = Depends(get_db)):
    """发送报价单给客户"""
    service = get_quotation_service()
    quotation = service.get_quotation(quotation_id)
    
    if not quotation:
        raise HTTPException(status_code=404, detail="报价单不存在")
    
    # 格式化文本
    text = service.format_quotation_text(quotation)
    
    # 获取客户
    customer = db.query(Customer).filter(
        Customer.id == quotation["customer_id"]
    ).first()
    
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    
    # 发送消息
    if not whatsapp_client:
        raise HTTPException(status_code=503, detail="WhatsApp 未连接")
    
    # 直接传递手机号，让 send_message 内部处理 JID 格式
    success = whatsapp_client.send_message(customer.phone, text)
    
    if success:
        return {"success": True, "message": "报价单已发送"}
    raise HTTPException(status_code=500, detail="发送失败")


# ============ 备忘 API ============

@app.get("/api/customers/{customer_id}/memos")
async def get_customer_memos(customer_id: int):
    """获取客户备忘"""
    service = get_memo_service()
    memos = service.get_memos(customer_id=customer_id)
    return {"success": True, "memos": memos}


@app.post("/api/customers/{customer_id}/memos")
async def create_memo(customer_id: int, request: dict):
    """创建备忘"""
    service = get_memo_service()
    memo_id = service.create_memo(
        customer_id=customer_id,
        content=request.get("content"),
        category=request.get("category", "general"),
        tags=request.get("tags", ""),
        created_by=request.get("created_by", "")
    )
    return {"success": True, "id": memo_id}


@app.delete("/api/memos/{memo_id}")
async def delete_memo(memo_id: int):
    """删除备忘"""
    service = get_memo_service()
    service.delete_memo(memo_id)
    return {"success": True}


# ============ 系统配置 API ============

class LLMConfigRequest(BaseModel):
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-3.5-turbo"


@app.get("/api/config/llm")
async def get_llm_config():
    """获取LLM配置（API Key隐藏）"""
    config = get_config_service()
    return {
        "success": True,
        "config": {
            "api_key_set": bool(config.get("llm_api_key")),
            "base_url": config.get("llm_base_url", "https://api.openai.com/v1"),
            "model": config.get("llm_model", "gpt-3.5-turbo")
        }
    }


@app.post("/api/config/llm")
async def set_llm_config(request: LLMConfigRequest):
    """设置LLM配置（API Key加密存储）"""
    config = get_config_service()
    config.set_llm_config(
        api_key=request.api_key,
        base_url=request.base_url,
        model=request.model
    )
    
    # 刷新LLM服务配置
    llm = get_llm_service()
    llm._refresh_config()
    
    return {"success": True, "message": "配置已保存"}


@app.delete("/api/config/llm")
async def delete_llm_config():
    """删除LLM配置"""
    config = get_config_service()
    config.delete("llm_api_key")
    config.delete("llm_base_url")
    config.delete("llm_model")
    return {"success": True, "message": "配置已删除"}


# ============ 定时发送计划 API ============

class CreateScheduleRequest(BaseModel):
    name: str
    message_template: str
    target_tags: List[str] = []
    target_category: Optional[str] = None
    schedule_time: str
    interval_seconds: int = 60


@app.get("/api/schedules")
async def get_schedules(status: Optional[str] = None):
    """获取发送计划列表"""
    service = get_scheduler_service()
    schedules = service.get_schedules(status)
    return {
        "success": True,
        "schedules": [{
            "id": s.id,
            "name": s.name,
            "message_template": s.message_template[:50] + "..." if len(s.message_template) > 50 else s.message_template,
            "target_tags": s.target_tags,
            "target_category": s.target_category,
            "schedule_time": s.schedule_time,
            "interval_seconds": s.interval_seconds,
            "status": s.status,
            "total_count": s.total_count,
            "sent_count": s.sent_count,
            "failed_count": s.failed_count,
            "created_at": s.created_at
        } for s in schedules]
    }


@app.post("/api/schedules")
async def create_schedule(request: CreateScheduleRequest, db: Session = Depends(get_db)):
    """创建发送计划"""
    service = get_scheduler_service()
    
    # 创建计划
    schedule_id = service.create_schedule(
        name=request.name,
        message_template=request.message_template,
        target_tags=request.target_tags,
        target_category=request.target_category,
        schedule_time=request.schedule_time,
        interval_seconds=request.interval_seconds
    )
    
    # 根据条件筛选客户
    query = db.query(Customer)
    
    if request.target_category:
        query = query.filter(Customer.category == request.target_category)
    
    # 如果有标签筛选，这里简化处理，实际应该根据标签关联表查询
    # 暂时返回所有符合条件的客户
    customers = query.all()
    
    # 准备任务
    customer_list = [{
        "id": c.id,
        "phone": c.phone,
        "name": c.name,
        "category": c.category
    } for c in customers]
    
    service.prepare_tasks(schedule_id, customer_list)
    
    return {
        "success": True,
        "message": f"计划创建成功，目标 {len(customer_list)} 个客户",
        "id": schedule_id
    }


@app.get("/api/schedules/{schedule_id}")
async def get_schedule_detail(schedule_id: int):
    """获取计划详情"""
    service = get_scheduler_service()
    schedule = service.get_schedule(schedule_id)
    tasks = service.get_tasks(schedule_id)
    
    if not schedule:
        raise HTTPException(status_code=404, detail="计划不存在")
    
    return {
        "success": True,
        "schedule": {
            "id": schedule.id,
            "name": schedule.name,
            "message_template": schedule.message_template,
            "target_tags": schedule.target_tags,
            "target_category": schedule.target_category,
            "schedule_time": schedule.schedule_time,
            "interval_seconds": schedule.interval_seconds,
            "status": schedule.status,
            "total_count": schedule.total_count,
            "sent_count": schedule.sent_count,
            "failed_count": schedule.failed_count,
            "created_at": schedule.created_at
        },
        "tasks": [{
            "id": t.id,
            "customer_id": t.customer_id,
            "customer_phone": t.customer_phone,
            "customer_name": t.customer_name,
            "message_content": t.message_content,
            "status": t.status,
            "sent_at": t.sent_at,
            "error_msg": t.error_msg
        } for t in tasks]
    }


@app.post("/api/schedules/{schedule_id}/execute")
async def execute_schedule_now(schedule_id: int):
    """立即执行计划"""
    runner = get_schedule_runner()
    runner.execute_now(schedule_id)
    return {"success": True, "message": "计划开始执行"}


@app.post("/api/schedules/{schedule_id}/pause")
async def pause_schedule(schedule_id: int):
    """暂停计划"""
    service = get_scheduler_service()
    service.pause_schedule(schedule_id)
    return {"success": True, "message": "计划已暂停"}


@app.post("/api/schedules/{schedule_id}/resume")
async def resume_schedule(schedule_id: int):
    """恢复计划"""
    service = get_scheduler_service()
    service.resume_schedule(schedule_id)
    return {"success": True, "message": "计划已恢复"}


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int):
    """删除计划"""
    service = get_scheduler_service()
    service.delete_schedule(schedule_id)
    return {"success": True, "message": "计划已删除"}


# ============ AI智能体管理 API ============

class AIAgentCreate(BaseModel):
    name: str
    description: Optional[str] = None
    system_prompt: str
    llm_provider_id: Optional[int] = None  # 关联的大模型提供商ID
    llm_model_id: Optional[str] = None  # 指定模型ID
    temperature: Optional[float] = None  # 覆盖默认温度
    max_tokens: Optional[int] = None  # 覆盖默认token数
    tag_ids: List[int] = []  # 绑定的客户标签ID列表
    is_default: bool = False
    priority: int = 0

class AIAgentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    llm_provider_id: Optional[int] = None
    llm_model_id: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tag_ids: Optional[List[int]] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    priority: Optional[int] = None

@app.get("/api/agents")
async def get_agents(db: Session = Depends(get_db)):
    """获取所有AI智能体"""
    agents = db.query(AIAgent).all()
    result = []
    for agent in agents:
        # 获取绑定的标签
        tag_bindings = db.query(AgentTagBinding).filter(AgentTagBinding.agent_id == agent.id).all()
        tag_ids = [b.tag_id for b in tag_bindings]
        tags = db.query(CustomerTag).filter(CustomerTag.id.in_(tag_ids)).all() if tag_ids else []
        
        # 获取大模型信息
        llm_provider = None
        if agent.llm_provider_id:
            provider = db.query(LLMProvider).filter(LLMProvider.id == agent.llm_provider_id).first()
            if provider:
                llm_provider = {
                    "id": provider.id,
                    "name": provider.name,
                    "provider_type": provider.provider_type,
                    "default_model": provider.default_model
                }
        
        result.append({
            "id": agent.id,
            "name": agent.name,
            "description": agent.description,
            "system_prompt": agent.system_prompt,
            "llm_provider_id": agent.llm_provider_id,
            "llm_provider": llm_provider,
            "llm_model_id": agent.llm_model_id,
            "temperature": agent.temperature,
            "max_tokens": agent.max_tokens,
            "is_active": agent.is_active,
            "is_default": agent.is_default,
            "priority": agent.priority,
            "tags": [{"id": t.id, "name": t.name, "color": t.color} for t in tags],
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
            "updated_at": agent.updated_at.isoformat() if agent.updated_at else None
        })
    return {"agents": result}

@app.post("/api/agents")
async def create_agent(agent_data: AIAgentCreate, db: Session = Depends(get_db)):
    """创建AI智能体"""
    # 如果设置为默认，取消其他默认智能体
    if agent_data.is_default:
        db.query(AIAgent).filter(AIAgent.is_default == True).update({"is_default": False})
    
    # 创建智能体
    agent = AIAgent(
        name=agent_data.name,
        description=agent_data.description,
        system_prompt=agent_data.system_prompt,
        llm_provider_id=agent_data.llm_provider_id,
        llm_model_id=agent_data.llm_model_id,
        temperature=agent_data.temperature,
        max_tokens=agent_data.max_tokens,
        is_default=agent_data.is_default,
        priority=agent_data.priority
    )
    db.add(agent)
    db.flush()  # 获取agent.id
    
    # 绑定标签 - 检查标签是否已被其他智能体绑定
    for tag_id in agent_data.tag_ids:
        # 检查该标签是否已被其他智能体绑定
        existing_binding = db.query(AgentTagBinding).filter(
            AgentTagBinding.tag_id == tag_id
        ).first()
        
        if existing_binding:
            # 如果标签已被绑定，删除旧绑定
            db.delete(existing_binding)
        
        binding = AgentTagBinding(agent_id=agent.id, tag_id=tag_id)
        db.add(binding)
    
    db.commit()
    db.refresh(agent)
    
    return {"success": True, "message": "智能体创建成功", "agent_id": agent.id}

@app.put("/api/agents/{agent_id}")
async def update_agent(agent_id: int, agent_data: AIAgentUpdate, db: Session = Depends(get_db)):
    """更新AI智能体"""
    agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="智能体不存在")
    
    # 如果设置为默认，取消其他默认智能体
    if agent_data.is_default:
        db.query(AIAgent).filter(AIAgent.is_default == True, AIAgent.id != agent_id).update({"is_default": False})
    
    # 更新字段
    update_data = agent_data.dict(exclude_unset=True)
    if "tag_ids" in update_data:
        tag_ids = update_data.pop("tag_ids")
        # 删除旧绑定
        db.query(AgentTagBinding).filter(AgentTagBinding.agent_id == agent_id).delete()
        # 添加新绑定 - 确保一个标签只能绑定一个智能体
        for tag_id in tag_ids:
            # 检查该标签是否已被其他智能体绑定
            existing_binding = db.query(AgentTagBinding).filter(
                AgentTagBinding.tag_id == tag_id,
                AgentTagBinding.agent_id != agent_id
            ).first()
            
            if existing_binding:
                # 如果标签已被其他智能体绑定，删除旧绑定
                db.delete(existing_binding)
            
            binding = AgentTagBinding(agent_id=agent_id, tag_id=tag_id)
            db.add(binding)
    
    for key, value in update_data.items():
        setattr(agent, key, value)
    
    agent.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(agent)
    
    return {"success": True, "message": "智能体更新成功"}

@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: int, db: Session = Depends(get_db)):
    """删除AI智能体"""
    agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="智能体不存在")
    
    db.delete(agent)
    db.commit()
    
    return {"success": True, "message": "智能体已删除"}

@app.post("/api/agents/{agent_id}/set-default")
async def set_default_agent(agent_id: int, db: Session = Depends(get_db)):
    """设置默认智能体"""
    agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="智能体不存在")
    
    # 取消其他默认智能体
    db.query(AIAgent).filter(AIAgent.is_default == True).update({"is_default": False})
    
    agent.is_default = True
    db.commit()
    
    return {"success": True, "message": "已设置为默认智能体"}


# ============ 客户标签管理 API ============

class CustomerTagCreate(BaseModel):
    name: str
    color: str = "#007bff"
    description: Optional[str] = None

class CustomerTagUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

@app.get("/api/customer-tags")
async def get_customer_tags(db: Session = Depends(get_db)):
    """获取所有客户标签"""
    tags = db.query(CustomerTag).filter(CustomerTag.is_active == True).all()
    return {
        "tags": [
            {
                "id": tag.id,
                "name": tag.name,
                "color": tag.color,
                "description": tag.description,
                "created_at": tag.created_at.isoformat() if tag.created_at else None
            }
            for tag in tags
        ]
    }

@app.post("/api/customer-tags")
async def create_customer_tag(tag_data: CustomerTagCreate, db: Session = Depends(get_db)):
    """创建客户标签"""
    # 检查标签名是否已存在
    existing = db.query(CustomerTag).filter(CustomerTag.name == tag_data.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="标签名称已存在")
    
    tag = CustomerTag(
        name=tag_data.name,
        color=tag_data.color,
        description=tag_data.description
    )
    db.add(tag)
    db.commit()
    db.refresh(tag)
    
    return {"success": True, "message": "标签创建成功", "tag_id": tag.id}

@app.put("/api/customer-tags/{tag_id}")
async def update_customer_tag(tag_id: int, tag_data: CustomerTagUpdate, db: Session = Depends(get_db)):
    """更新客户标签"""
    tag = db.query(CustomerTag).filter(CustomerTag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="标签不存在")
    
    # 检查名称是否重复
    if tag_data.name and tag_data.name != tag.name:
        existing = db.query(CustomerTag).filter(CustomerTag.name == tag_data.name).first()
        if existing:
            raise HTTPException(status_code=400, detail="标签名称已存在")
    
    for key, value in tag_data.dict(exclude_unset=True).items():
        setattr(tag, key, value)
    
    db.commit()
    db.refresh(tag)
    
    return {"success": True, "message": "标签更新成功"}

@app.delete("/api/customer-tags/{tag_id}")
async def delete_customer_tag(tag_id: int, db: Session = Depends(get_db)):
    """删除客户标签（软删除）"""
    tag = db.query(CustomerTag).filter(CustomerTag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="标签不存在")
    
    tag.is_active = False
    db.commit()
    
    return {"success": True, "message": "标签已删除"}


# ============ 客户标签关联 API ============

@app.post("/api/customers/{customer_id}/tags/{tag_id}")
async def add_tag_to_customer(customer_id: int, tag_id: int, db: Session = Depends(get_db)):
    """为客户添加标签"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    
    tag = db.query(CustomerTag).filter(CustomerTag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="标签不存在")
    
    # 检查是否已关联
    existing = db.query(CustomerTagAssociation).filter(
        CustomerTagAssociation.customer_id == customer_id,
        CustomerTagAssociation.tag_id == tag_id
    ).first()
    
    if existing:
        return {"success": True, "message": "标签已存在"}
    
    association = CustomerTagAssociation(customer_id=customer_id, tag_id=tag_id)
    db.add(association)
    db.commit()
    
    return {"success": True, "message": "标签添加成功"}

@app.delete("/api/customers/{customer_id}/tags/{tag_id}")
async def remove_tag_from_customer(customer_id: int, tag_id: int, db: Session = Depends(get_db)):
    """移除客户标签"""
    association = db.query(CustomerTagAssociation).filter(
        CustomerTagAssociation.customer_id == customer_id,
        CustomerTagAssociation.tag_id == tag_id
    ).first()
    
    if association:
        db.delete(association)
        db.commit()
    
    return {"success": True, "message": "标签已移除"}


# ============ 大模型管理 API ============

class LLMProviderCreate(BaseModel):
    name: str
    provider_type: str  # deepseek, openai, claude
    api_key: str
    base_url: Optional[str] = None
    default_model: str
    temperature: float = 0.7
    max_tokens: int = 500
    timeout: int = 30
    is_default: bool = False

class LLMProviderUpdate(BaseModel):
    name: Optional[str] = None
    provider_type: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    default_model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout: Optional[int] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None

@app.get("/api/llm-providers")
async def get_llm_providers(db: Session = Depends(get_db)):
    """获取所有大模型提供商"""
    providers = db.query(LLMProvider).filter(LLMProvider.is_active == True).all()
    return {
        "providers": [
            {
                "id": p.id,
                "name": p.name,
                "provider_type": p.provider_type,
                "base_url": p.base_url,
                "default_model": p.default_model,
                "temperature": p.temperature,
                "max_tokens": p.max_tokens,
                "timeout": p.timeout,
                "is_active": p.is_active,
                "is_default": p.is_default,
                "created_at": p.created_at.isoformat() if p.created_at else None
            }
            for p in providers
        ]
    }

@app.post("/api/llm-providers")
async def create_llm_provider(provider_data: LLMProviderCreate, db: Session = Depends(get_db)):
    """创建大模型提供商"""
    # 如果设置为默认，取消其他默认
    if provider_data.is_default:
        db.query(LLMProvider).filter(LLMProvider.is_default == True).update({"is_default": False})
    
    provider = LLMProvider(
        name=provider_data.name,
        provider_type=provider_data.provider_type,
        api_key=provider_data.api_key,
        base_url=provider_data.base_url,
        default_model=provider_data.default_model,
        temperature=provider_data.temperature,
        max_tokens=provider_data.max_tokens,
        timeout=provider_data.timeout,
        is_default=provider_data.is_default
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    
    return {"success": True, "message": "大模型提供商创建成功", "provider_id": provider.id}

@app.put("/api/llm-providers/{provider_id}")
async def update_llm_provider(provider_id: int, provider_data: LLMProviderUpdate, db: Session = Depends(get_db)):
    """更新大模型提供商"""
    provider = db.query(LLMProvider).filter(LLMProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="提供商不存在")
    
    # 如果设置为默认，取消其他默认
    if provider_data.is_default:
        db.query(LLMProvider).filter(LLMProvider.is_default == True, LLMProvider.id != provider_id).update({"is_default": False})
    
    for key, value in provider_data.dict(exclude_unset=True).items():
        setattr(provider, key, value)
    
    provider.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(provider)
    
    return {"success": True, "message": "大模型提供商更新成功"}

@app.delete("/api/llm-providers/{provider_id}")
async def delete_llm_provider(provider_id: int, db: Session = Depends(get_db)):
    """删除大模型提供商（软删除）"""
    provider = db.query(LLMProvider).filter(LLMProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="提供商不存在")
    
    provider.is_active = False
    db.commit()
    
    return {"success": True, "message": "大模型提供商已删除"}

@app.post("/api/llm-providers/{provider_id}/set-default")
async def set_default_llm_provider(provider_id: int, db: Session = Depends(get_db)):
    """设置默认大模型提供商"""
    provider = db.query(LLMProvider).filter(LLMProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="提供商不存在")
    
    db.query(LLMProvider).filter(LLMProvider.is_default == True).update({"is_default": False})
    provider.is_default = True
    db.commit()
    
    return {"success": True, "message": "已设置为默认大模型提供商"}

@app.post("/api/llm-providers/{provider_id}/test")
async def test_llm_provider(provider_id: int, db: Session = Depends(get_db)):
    """测试大模型提供商连接"""
    provider = db.query(LLMProvider).filter(LLMProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="提供商不存在")
    
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{provider.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {provider.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": provider.default_model,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 10
                },
                timeout=provider.timeout
            )
            
            if response.status_code == 200:
                return {"success": True, "message": "连接测试成功"}
            else:
                return {"success": False, "message": f"连接测试失败: {response.status_code}"}
    except Exception as e:
        return {"success": False, "message": f"连接测试失败: {str(e)}"}


# ============ 提示词优化 API ============

class PromptOptimizeRequest(BaseModel):
    prompt: str
    provider_id: Optional[int] = None

@app.post("/api/prompts/optimize")
async def optimize_prompt(request: PromptOptimizeRequest, db: Session = Depends(get_db)):
    """使用大模型优化提示词"""
    if not request.prompt or len(request.prompt.strip()) < 10:
        return {"success": False, "message": "提示词太短，至少需要10个字符"}
    
    try:
        # 获取提供商
        provider = None
        if request.provider_id:
            provider = db.query(LLMProvider).filter(
                LLMProvider.id == request.provider_id,
                LLMProvider.is_active == True
            ).first()
        
        # 如果没有指定或找不到，使用默认提供商
        if not provider:
            provider = db.query(LLMProvider).filter(
                LLMProvider.is_default == True,
                LLMProvider.is_active == True
            ).first()
        
        # 如果没有配置任何提供商，使用环境变量配置
        if not provider:
            import os
            api_key = os.getenv("OPENAI_API_KEY", "")
            base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
            model = os.getenv("LLM_MODEL", "gpt-3.5-turbo")
            
            if not api_key:
                return {"success": False, "message": "未配置大模型提供商，请先配置"}
        else:
            api_key = provider.api_key
            base_url = provider.base_url or "https://api.openai.com/v1"
            model = provider.default_model
        
        # 构建优化提示词的系统提示
        system_prompt = """你是一位专业的AI提示词工程师。你的任务是帮助用户优化他们的AI提示词。

请分析用户提供的提示词，并提供以下优化：
1. **优化后的提示词** - 改进后的完整提示词
2. **优化说明** - 简要说明做了哪些改进

优化原则：
- 使提示词更清晰、具体
- 添加必要的上下文和约束
- 明确AI的角色和任务
- 使用结构化的格式（如Markdown）
- 保持简洁但完整

请用中文回复，格式如下：

【优化后的提示词】
（优化后的完整提示词）

【优化说明】
（简要说明改进点）"""
        
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"请优化以下提示词：\n\n{request.prompt}"}
                    ],
                    "temperature": 0.7,
                    "max_tokens": 1000
                },
                timeout=30.0
            )
            
            if response.status_code == 200:
                result = response.json()
                optimized_text = result["choices"][0]["message"]["content"]
                return {
                    "success": True,
                    "optimized_prompt": optimized_text,
                    "provider_used": provider.name if provider else "默认配置"
                }
            else:
                return {
                    "success": False,
                    "message": f"优化失败: {response.status_code} - {response.text}"
                }
                
    except Exception as e:
        return {"success": False, "message": f"优化失败: {str(e)}"}


# ============ 自动打标签规则 API ============

class AutoTagRuleCreate(BaseModel):
    name: str
    tag_id: int
    condition_type: str  # message_received, quote_requested, keyword_match, ai_detected
    condition_config: Optional[Dict] = {}
    priority: int = 0

class AutoTagRuleUpdate(BaseModel):
    name: Optional[str] = None
    tag_id: Optional[int] = None
    condition_type: Optional[str] = None
    condition_config: Optional[Dict] = None
    is_active: Optional[bool] = None
    priority: Optional[int] = None

@app.get("/api/auto-tag-rules")
async def get_auto_tag_rules(db: Session = Depends(get_db)):
    """获取所有自动打标签规则"""
    rules = db.query(AutoTagRule).filter(AutoTagRule.is_active == True).order_by(AutoTagRule.priority.desc()).all()
    return {
        "rules": [
            {
                "id": r.id,
                "name": r.name,
                "tag_id": r.tag_id,
                "tag": {
                    "id": r.tag.id,
                    "name": r.tag.name,
                    "color": r.tag.color
                } if r.tag else None,
                "condition_type": r.condition_type,
                "condition_config": r.condition_config,
                "is_active": r.is_active,
                "priority": r.priority,
                "created_at": r.created_at.isoformat() if r.created_at else None
            }
            for r in rules
        ]
    }

@app.post("/api/auto-tag-rules")
async def create_auto_tag_rule(rule_data: AutoTagRuleCreate, db: Session = Depends(get_db)):
    """创建自动打标签规则"""
    # 检查标签是否存在
    tag = db.query(CustomerTag).filter(CustomerTag.id == rule_data.tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="标签不存在")
    
    rule = AutoTagRule(
        name=rule_data.name,
        tag_id=rule_data.tag_id,
        condition_type=rule_data.condition_type,
        condition_config=rule_data.condition_config,
        priority=rule_data.priority
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    
    return {"success": True, "message": "规则创建成功", "rule_id": rule.id}

@app.put("/api/auto-tag-rules/{rule_id}")
async def update_auto_tag_rule(rule_id: int, rule_data: AutoTagRuleUpdate, db: Session = Depends(get_db)):
    """更新自动打标签规则"""
    rule = db.query(AutoTagRule).filter(AutoTagRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")
    
    for key, value in rule_data.dict(exclude_unset=True).items():
        setattr(rule, key, value)
    
    rule.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(rule)
    
    return {"success": True, "message": "规则更新成功"}

@app.delete("/api/auto-tag-rules/{rule_id}")
async def delete_auto_tag_rule(rule_id: int, db: Session = Depends(get_db)):
    """删除自动打标签规则（软删除）"""
    rule = db.query(AutoTagRule).filter(AutoTagRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")
    
    rule.is_active = False
    db.commit()
    
    return {"success": True, "message": "规则已删除"}

@app.post("/api/customers/{customer_id}/apply-auto-tags")
async def apply_auto_tags_to_customer(customer_id: int, message_content: Optional[str] = None, db: Session = Depends(get_db)):
    """为客户应用自动打标签规则"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    
    # 获取所有启用的自动标签规则
    rules = db.query(AutoTagRule).filter(AutoTagRule.is_active == True).order_by(AutoTagRule.priority.desc()).all()
    
    applied_tags = []
    
    for rule in rules:
        should_apply = False
        
        if rule.condition_type == "new_customer":
            # 新客户（首次进入通讯录）- 检查客户的消息数量是否为0（只有系统消息或没有消息）
            message_count = db.query(Message).filter(Message.customer_id == customer_id).count()
            # 如果客户消息很少（<=1条），认为是新客户
            should_apply = message_count <= 1
            
        elif rule.condition_type == "message_received":
            # 客户有回复就打标签
            message_count = db.query(Message).filter(Message.customer_id == customer_id, Message.direction == "incoming").count()
            should_apply = message_count > 0
            
        elif rule.condition_type == "quote_requested":
            # 客户要求报价（通过关键词检测或AI识别）
            if message_content:
                quote_keywords = ["报价", "价格", "多少钱", "费用", "cost", "price", "quote"]
                should_apply = any(keyword in message_content.lower() for keyword in quote_keywords)
        
        elif rule.condition_type == "keyword_match":
            # 关键词匹配
            if message_content and rule.condition_config.get("keywords"):
                keywords = rule.condition_config["keywords"]
                should_apply = any(keyword in message_content.lower() for keyword in keywords)
        
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
    
    db.commit()
    
    return {
        "success": True,
        "applied_tags": applied_tags,
        "message": f"成功应用 {len(applied_tags)} 个标签"
    }

@app.post("/api/auto-tag-rules/{rule_id}/execute")
async def execute_auto_tag_rule(rule_id: int, db: Session = Depends(get_db)):
    """手动执行自动打标签规则 - 为所有客户应用此规则"""
    rule = db.query(AutoTagRule).filter(AutoTagRule.id == rule_id, AutoTagRule.is_active == True).first()
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在或已禁用")
    
    # 获取所有客户
    customers = db.query(Customer).all()
    
    applied_count = 0
    skipped_count = 0
    
    for customer in customers:
        should_apply = False
        
        if rule.condition_type == "new_customer":
            # 新客户 - 消息数量<=1
            message_count = db.query(Message).filter(Message.customer_id == customer.id).count()
            should_apply = message_count <= 1
            
        elif rule.condition_type == "message_received":
            # 客户有回复
            message_count = db.query(Message).filter(
                Message.customer_id == customer.id, 
                Message.direction == "incoming"
            ).count()
            should_apply = message_count > 0
            
        elif rule.condition_type == "quote_requested":
            # 客户要求报价 - 检查最近的消息
            recent_messages = db.query(Message).filter(
                Message.customer_id == customer.id,
                Message.direction == "incoming"
            ).order_by(Message.created_at.desc()).limit(5).all()
            
            quote_keywords = ["报价", "价格", "多少钱", "费用", "cost", "price", "quote"]
            for msg in recent_messages:
                if any(keyword in msg.content.lower() for keyword in quote_keywords):
                    should_apply = True
                    break
        
        elif rule.condition_type == "keyword_match":
            # 关键词匹配
            if rule.condition_config.get("keywords"):
                keywords = rule.condition_config["keywords"]
                recent_messages = db.query(Message).filter(
                    Message.customer_id == customer.id,
                    Message.direction == "incoming"
                ).order_by(Message.created_at.desc()).limit(10).all()
                
                for msg in recent_messages:
                    if any(keyword in msg.content.lower() for keyword in keywords):
                        should_apply = True
                        break
        
        if should_apply:
            # 检查是否已经有这个标签
            existing = db.query(CustomerTagAssociation).filter(
                CustomerTagAssociation.customer_id == customer.id,
                CustomerTagAssociation.tag_id == rule.tag_id
            ).first()
            
            if not existing:
                # 添加标签
                association = CustomerTagAssociation(customer_id=customer.id, tag_id=rule.tag_id)
                db.add(association)
                
                # 记录日志
                log = CustomerTagLog(
                    customer_id=customer.id,
                    tag_id=rule.tag_id,
                    action="add",
                    source="auto_rule",
                    source_id=rule.id
                )
                db.add(log)
                
                applied_count += 1
            else:
                skipped_count += 1
    
    db.commit()
    
    return {
        "success": True,
        "message": f"规则执行完成: 成功打标签 {applied_count} 个客户, 跳过 {skipped_count} 个已存在标签的客户",
        "applied_count": applied_count,
        "skipped_count": skipped_count
    }


# ============ 后台任务 ============

async def process_incoming_messages():
    """处理新消息的后台任务 - 已合并到message_syncer中"""
    # 此功能已移至 MessageSyncer.start_polling()
    # 保留此函数以避免导入错误
    pass


if __name__ == "__main__":
    import uvicorn
    
    # 启动后台任务
    asyncio.create_task(process_incoming_messages())
    
    uvicorn.run(
        "main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", 8000)),
        reload=os.getenv("DEBUG", "false").lower() == "true"
    )
