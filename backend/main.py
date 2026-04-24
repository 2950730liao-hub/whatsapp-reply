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

# 配置日志
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/tmp/whatsapp_crm.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, BackgroundTasks, Request, Form, File, UploadFile, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session, joinedload
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from database import init_db, get_db, SessionLocal, Customer, Message, Conversation, Agent, CommunicationPlan, AIAgent, CustomerTag, CustomerTagAssociation, AgentTagBinding, AgentKnowledgeBinding, LLMProvider, LLMModel, AutoTagRule, CustomerTagLog

# 使用新的 WhatsApp 客户端管理器
from whatsapp_adapter import WhatsAppClientManager, create_client_manager
from communication_service import CommunicationService, NotificationService
from llm_service import get_llm_service
from knowledge_base import get_knowledge_base
from quotation_service import get_quotation_service
from memo_service import get_memo_service
from config_service import get_config_service
from scheduler_service import get_scheduler_service
from schedule_runner import get_schedule_runner
from admin_notify import get_admin_notify_db
from notify_service import get_notify_service


# 全局状态
whatsapp_client = None
message_syncer = None
active_websockets: set = set()

# 并发安全锁
_ws_lock = threading.Lock()
_client_lock = threading.Lock()

# WhatsApp 客户端管理器
client_manager: WhatsAppClientManager = None


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


async def recover_unprocessed_messages():
    """服务启动后，检查并补回复崩溃期间未处理的消息"""
    from datetime import timedelta
    from database import SessionLocal, Message, Customer
    from communication_service import CommunicationService
    
    # 等待 WhatsApp 连接稳定
    await asyncio.sleep(30)
    
    db = SessionLocal()
    try:
        # 查询最近 30 分钟内未处理的 incoming 消息
        since = datetime.utcnow() - timedelta(minutes=30)
        unprocessed = db.query(Message).filter(
            Message.direction == "incoming",
            Message.is_processed == False,
            Message.created_at >= since
        ).order_by(Message.created_at.desc()).all()
        
        if not unprocessed:
            print("[Recover] 未发现未处理消息，无需补回复")
            return
        
        # 按客户分组，每个客户只处理最新一条
        customer_latest = {}
        for msg in unprocessed:
            if msg.customer_id not in customer_latest:
                customer_latest[msg.customer_id] = msg
        
        print(f"[Recover] 发现 {len(customer_latest)} 个客户有未处理消息，开始补回复...")
        
        # 逐个处理
        comm_service = CommunicationService(None, whatsapp_client)
        for customer_id, msg in customer_latest.items():
            try:
                # 重新查询确保对象在有效 session 中
                fresh_msg = db.query(Message).filter(Message.id == msg.id).first()
                fresh_customer = db.query(Customer).filter(Customer.id == customer_id).first()
                if not fresh_msg or not fresh_customer:
                    continue
                
                # 再次检查是否已被处理
                if fresh_msg.is_processed:
                    print(f"[Recover] 消息 {msg.id} 已被处理，跳过")
                    continue
                
                print(f"[Recover] 为客户 {fresh_customer.phone} 补回复消息 {msg.id}: {fresh_msg.content[:30]}...")
                
                # 调用处理逻辑（在线程池中运行避免阻塞事件循环）
                await asyncio.to_thread(comm_service.handle_incoming_message, fresh_msg, fresh_customer)
                
                # 等待一下避免并发过高
                await asyncio.sleep(2)
                
            except Exception as e:
                print(f"[Recover] 补回复消息 {msg.id} 失败: {e}")
                import traceback
                traceback.print_exc()
        
        print("[Recover] 补回复任务完成")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global whatsapp_client, message_syncer, client_manager
    
    # 启动时初始化
    print("🚀 启动 WhatsApp 智能客户系统...")
    
    # 初始化数据库
    init_db()
    print("✅ 数据库初始化完成")
    
    # 初始化 WhatsApp 客户端管理器
    client_manager = create_client_manager()
    whatsapp_client = client_manager.initialize()
    
    # 注册消息处理器
    from communication_service import CommunicationService
    comm_service = CommunicationService(None, whatsapp_client)
    client_manager.register_message_handler(comm_service.handle_incoming_message)
    
    # 获取 message_syncer（CLI 模式下可用）
    message_syncer = client_manager.message_syncer
    
    # CLI 模式下启动定时发送执行器
    if client_manager.is_cli and whatsapp_client.is_authenticated():
        runner = get_schedule_runner(whatsapp_client)
        runner.start()
        print("✅ 定时发送执行器已启动")
    
    # 登录成功后自动同步通讯录
    if whatsapp_client and whatsapp_client.is_connected():
        try:
            from contact_sync_service import get_contact_sync_service
            sync_service = get_contact_sync_service()
            stats = sync_service.auto_sync_on_login()
            print(f"✅ 通讯录同步完成: 新增 {stats['new_customers']}, 更新 {stats['updated_customers']}, 清理 {stats['removed_customers']}")
        except Exception as e:
            print(f"⚠️ 通讯录同步失败: {e}")
    
    # 启动通讯录监控服务（自动检测变化）
    try:
        from contact_monitor_service import get_contact_monitor_service
        monitor_service = get_contact_monitor_service()
        monitor_service.start()
        print("✅ 通讯录监控服务已启动（30秒轮询）")
    except Exception as e:
        print(f"⚠️ 通讯录监控服务启动失败: {e}")
    
    # 启动未处理消息恢复任务（服务崩溃重启后补回复）
    asyncio.create_task(recover_unprocessed_messages())
    print("✅ 未处理消息恢复任务已启动（30秒后执行）")
    
    yield
    
    # 关闭时清理
    print("🛑 关闭系统...")
    
    # 停止通讯录监控
    try:
        from contact_monitor_service import get_contact_monitor_service
        monitor_service = get_contact_monitor_service()
        monitor_service.stop()
        print("✅ 通讯录监控服务已停止")
    except:
        pass
    
    # 使用管理器统一关闭
    if client_manager:
        client_manager.shutdown()
    
    # 停止定时任务
    runner = get_schedule_runner()
    runner.stop()
    
    # 关闭 WebSocket 连接
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

# 挂载知识库文件目录（上传的图片/文档可被直接访问）
_kb_files_dir = os.path.join(os.path.dirname(__file__), "data", "knowledge_files")
os.makedirs(_kb_files_dir, exist_ok=True)
app.mount("/knowledge-files", StaticFiles(directory=_kb_files_dir), name="knowledge-files")

@app.get("/")
async def serve_index():
    """服务首页"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "WhatsApp CRM API is running"}

# CORS 配置
allowed_origins = [
    "http://localhost:3000",
    "http://localhost:8000",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:8000",
    os.getenv("FRONTEND_URL", "http://localhost:3000"),
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    max_age=600,
)

# 全局异常处理中间件
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    # 生产环境不返回错误详情
    if os.getenv("ENVIRONMENT", "development") == "production":
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"}
        )
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
        ws_copy = list(active_websockets)
    
    for ws in ws_copy:
        try:
            await ws.send_json({"type": "new_message", "data": message})
        except Exception as e:
            logger.debug(f"WebSocket 广播失败，标记移除: {e}")
            disconnected.append(ws)
    
    if disconnected:
        with _ws_lock:
            for ws in disconnected:
                active_websockets.discard(ws)


# ============ 认证相关 API ============

@app.get("/api/auth/status")
async def get_auth_status():
    """获取 WhatsApp 登录状态"""
    if not whatsapp_client:
        return {
            "connected": False, 
            "error": "客户端未初始化",
            "backend": client_manager.get_backend_name() if client_manager else "unknown"
        }
    
    try:
        # 获取当前登录用户信息
        current_user = None
        try:
            current_user = whatsapp_client.get_current_user()
        except:
            pass
        
        if client_manager and client_manager.is_neonize:
            # Neonize 方案
            from neonize_client import get_current_qr
            qr_code = get_current_qr()
            
            return {
                "connected": whatsapp_client.connected,
                "logged_in": whatsapp_client.connected,
                "backend": "neonize",
                "qr_code": qr_code,  # 返回二维码数据
                "message": "请扫描二维码登录" if not whatsapp_client.connected else "已连接",
                "current_user": current_user  # 当前登录的号码
            }
        else:
            # CLI 方案
            status = whatsapp_client.auth_status()
            return {
                "connected": status.get("connected", False),
                "logged_in": status.get("logged_in", False),
                "database": status.get("database", {}),
                "backend": "cli",
                "current_user": current_user  # 当前登录的号码
            }
    except Exception as e:
        return {
            "connected": False, 
            "error": str(e),
            "backend": client_manager.get_backend_name() if client_manager else "unknown"
        }


@app.post("/api/auth/refresh-qr")
async def refresh_qr_code():
    """刷新二维码 - 清除当前二维码等待新生成"""
    if not client_manager or not client_manager.is_neonize:
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


@app.get("/api/auth/qr-image")
async def get_qr_image():
    """获取二维码图片（base64）"""
    try:
        from neonize_client import get_current_qr
        qr_data = get_current_qr()
        
        if not qr_data:
            return {
                "success": False,
                "message": "暂无二维码，请等待生成"
            }
        
        # 使用 segno 生成二维码图片
        import segno
        import io
        import base64
        
        qr = segno.make(qr_data, error='h')
        buffer = io.BytesIO()
        qr.save(buffer, kind='png', scale=10, border=4)
        buffer.seek(0)
        
        img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        
        return {
            "success": True,
            "qr_image": f"data:image/png;base64,{img_base64}",
            "message": "二维码已生成"
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"生成二维码失败: {str(e)}"
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
                        
                        # 重新启动消息同步（仅 CLI 后端需要）
                        if not client_manager.is_neonize:
                            global message_syncer
                            with _client_lock:
                                if message_syncer:
                                    message_syncer.stop()
                                from whatsapp_client import MessageSyncer
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
    tag_ids: Optional[List[int]] = []  # 要分配的标签ID列表
    duplicate_strategy: str = "skip"  # skip / update

@app.post("/api/customers/batch-import")
async def batch_import_customers(
    data: BatchImportCustomer,
    db: Session = Depends(get_db)
):
    """批量导入客户 - 支持标签分配 + 重复处理策略"""
    try:
        imported = []
        updated = []
        skipped = []
        failed = []
        
        # 预加载标签
        valid_tags = []
        if data.tag_ids:
            valid_tags = db.query(CustomerTag).filter(
                CustomerTag.id.in_(data.tag_ids),
                CustomerTag.is_active == True
            ).all()
        
        for item in data.customers:
            phone = item.get("phone", "").strip()
            name = item.get("name", "").strip()
            note = item.get("note", "").strip()
            
            # 验证手机号
            if not phone:
                failed.append({"phone": phone, "reason": "手机号为空"})
                continue
            
            # 清理手机号（只保留数字和+）
            phone_clean = ''.join(c for c in phone if c.isdigit() or c == '+')
            if phone_clean.startswith('+'):
                phone_clean = phone_clean[1:]
            
            if not phone_clean or len(phone_clean) < 7:
                failed.append({"phone": phone, "reason": "手机号格式错误"})
                continue
            
            # 检查是否已存在
            existing = db.query(Customer).filter(Customer.phone == phone_clean).first()
            if existing:
                if data.duplicate_strategy == "update":
                    # 更新姓名
                    if name and name != existing.name:
                        existing.name = name
                    # 追加标签
                    for tag in valid_tags:
                        tag_exists = db.query(CustomerTagAssociation).filter(
                            CustomerTagAssociation.customer_id == existing.id,
                            CustomerTagAssociation.tag_id == tag.id
                        ).first()
                        if not tag_exists:
                            db.add(CustomerTagAssociation(customer_id=existing.id, tag_id=tag.id))
                    updated.append({"phone": phone_clean, "name": existing.name})
                else:
                    skipped.append({"phone": phone_clean, "reason": "客户已存在（已跳过）"})
                continue
            
            # 创建新客户
            customer = Customer(
                phone=phone_clean,
                name=name or phone_clean,
                category="new",
                status="active"
            )
            db.add(customer)
            db.flush()  # 获取 customer.id
            
            # 分配标签
            for tag in valid_tags:
                db.add(CustomerTagAssociation(customer_id=customer.id, tag_id=tag.id))
            
            imported.append({"phone": phone_clean, "name": name or phone_clean})
        
        db.commit()
        
        return {
            "success": True,
            "imported_count": len(imported),
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "imported": imported,
            "updated": updated,
            "skipped": skipped,
            "failed": failed
        }
    
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"导入失败: {str(e)}")


@app.get("/api/customers/import-template")
async def download_import_template():
    """下载批量导入 CSV 模板"""
    import io
    from fastapi.responses import StreamingResponse
    
    # 生成 CSV 内容（BOM 头确保 Excel 正确识别中文）
    content = "\ufeff手机号,姓名,备注\n"
    content += "8613800138000,张三,VIP客户\n"
    content += "8618612345678,李四,\n"
    content += "8615900001234,王五,潜在客户\n"
    
    buffer = io.BytesIO(content.encode("utf-8-sig"))
    
    return StreamingResponse(
        buffer,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=import_template.csv"}
    )


@app.post("/api/auth/sync-contacts")
async def sync_contacts():
    """
    同步通讯录到 CRM
    - 从 Neonize 数据库读取真实通讯录
    - 清理不在通讯录中的客户
    - 更新客户名称（使用通讯录 push_name）
    """
    try:
        if not whatsapp_client or not whatsapp_client.is_authenticated():
            return {"success": False, "message": "WhatsApp 未登录"}
        
        # 使用通讯录监控服务强制同步
        from contact_monitor_service import get_contact_monitor_service
        monitor_service = get_contact_monitor_service()
        stats = monitor_service.force_sync()
        
        return {
            "success": True,
            "message": f"通讯录同步完成",
            "login_number": stats['login_number'],
            "total_contacts": stats['total_contacts'],
            "new_customers": stats['new_customers'],
            "updated_customers": stats['updated_customers'],
            "removed_customers": stats['removed_customers'],
            "skipped_self": stats['skipped_self'],
            "errors": stats['errors']
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "message": f"同步失败: {str(e)}"}


@app.get("/api/contacts/summary")
async def get_contacts_summary():
    """获取通讯录摘要信息"""
    try:
        from contact_monitor_service import get_contact_monitor_service
        monitor_service = get_contact_monitor_service()
        summary = monitor_service.get_contacts_summary()
        
        return {
            "success": True,
            "summary": summary
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


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
        # 使用 joinedload 预加载标签关联，避免 N+1 查询
        query = db.query(Customer).options(
            joinedload(Customer.tags).joinedload(CustomerTagAssociation.tag)
        )
        
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


@app.get("/api/customers/{customer_id}/detail")
async def get_customer_detail(customer_id: int, db: Session = Depends(get_db)):
    """获取客户详细信息（包含标签、消息统计等）"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    
    # 获取标签
    tags = []
    try:
        for tag_assoc in customer.tags:
            if tag_assoc and tag_assoc.tag and tag_assoc.tag.is_active:
                tags.append({
                    "id": tag_assoc.tag.id,
                    "name": tag_assoc.tag.name,
                    "color": tag_assoc.tag.color
                })
    except:
        pass
    
    # 获取消息统计
    from database import Message
    total_messages = db.query(Message).filter(Message.customer_id == customer_id).count()
    incoming_messages = db.query(Message).filter(
        Message.customer_id == customer_id,
        Message.direction == "incoming"
    ).count()
    outgoing_messages = db.query(Message).filter(
        Message.customer_id == customer_id,
        Message.direction == "outgoing"
    ).count()
    
    # 获取最后联系时间
    last_message = db.query(Message).filter(
        Message.customer_id == customer_id
    ).order_by(Message.created_at.desc()).first()
    
    # 获取会话状态
    from database import Conversation
    conversation = db.query(Conversation).filter(
        Conversation.customer_id == customer_id
    ).first()
    
    return {
        "id": customer.id,
        "phone": customer.phone,
        "name": customer.name,
        "category": customer.category,
        "status": customer.status,
        "tags": tags,
        "statistics": {
            "total_messages": total_messages,
            "incoming_messages": incoming_messages,
            "outgoing_messages": outgoing_messages,
            "last_contact": last_message.created_at.isoformat() if last_message else None
        },
        "conversation": {
            "id": conversation.id if conversation else None,
            "status": conversation.status if conversation else None,
            "last_message_at": conversation.last_message_at.isoformat() if conversation and conversation.last_message_at else None
        } if conversation else None,
        "created_at": customer.created_at.isoformat() if customer.created_at else None,
        "updated_at": customer.updated_at.isoformat() if customer.updated_at else None
    }


@app.get("/api/customers/{customer_id}", response_model=CustomerResponse)
async def get_customer(customer_id: int, db: Session = Depends(get_db)):
    """获取客户基本信息"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    return customer


class CustomerUpdateRequest(BaseModel):
    """更新客户信息请求"""
    name: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    tag_ids: Optional[List[int]] = None  # 标签ID列表


@app.put("/api/customers/{customer_id}")
async def update_customer(
    customer_id: int,
    request: CustomerUpdateRequest,
    db: Session = Depends(get_db)
):
    """更新客户信息（包含标签）"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    
    # 更新基本信息
    if request.name is not None:
        customer.name = request.name
    if request.category is not None:
        customer.category = request.category
    if request.status is not None:
        customer.status = request.status
    
    # 更新标签
    if request.tag_ids is not None:
        # 删除旧标签关联
        db.query(CustomerTagAssociation).filter(
            CustomerTagAssociation.customer_id == customer_id
        ).delete()
        
        # 添加新标签关联
        for tag_id in request.tag_ids:
            # 验证标签存在
            tag = db.query(CustomerTag).filter(
                CustomerTag.id == tag_id,
                CustomerTag.is_active == True
            ).first()
            if tag:
                assoc = CustomerTagAssociation(
                    customer_id=customer_id,
                    tag_id=tag_id
                )
                db.add(assoc)
    
    customer.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(customer)
    
    return {"success": True, "message": "客户信息已更新"}


@app.put("/api/customers/{customer_id}/category")
async def update_customer_category(
    customer_id: int,
    category: str,
    db: Session = Depends(get_db)
):
    """更新客户分类（快捷接口）"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    
    customer.category = category
    customer.updated_at = datetime.utcnow()
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


@app.post("/api/customers/{customer_id}/conversation/toggle")
async def toggle_conversation_status(customer_id: int, db: Session = Depends(get_db)):
    """切换客户会话状态（bot <-> handover）"""
    conversation = db.query(Conversation).filter(
        Conversation.customer_id == customer_id
    ).first()
    
    if not conversation:
        # 创建新会话，默认 bot 状态
        conversation = Conversation(
            customer_id=customer_id,
            status="bot"
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return {
            "success": True,
            "message": "会话已创建，当前状态: AI接管",
            "status": "bot"
        }
    
    # 切换状态
    if conversation.status == "handover":
        conversation.status = "bot"
        message = "已切换为 AI 接管"
    else:
        conversation.status = "handover"
        message = "已切换为 人工接管"
    
    db.commit()
    
    return {
        "success": True,
        "message": message,
        "status": conversation.status
    }


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


@app.post("/api/customers/{customer_id}/messages/send-media")
async def send_media_message(
    customer_id: int,
    media_type: str = Form(...),  # image, video, document, audio
    caption: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """发送多媒体消息给客户"""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    
    if not whatsapp_client or not whatsapp_client.is_authenticated():
        raise HTTPException(status_code=400, detail="WhatsApp 未登录")
    
    # 保存上传的文件
    import tempfile
    import os
    
    temp_dir = tempfile.gettempdir()
    file_path = os.path.join(temp_dir, file.filename)
    
    try:
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        
        # 根据类型发送
        success = False
        if media_type == "image":
            success = whatsapp_client.send_image(customer.phone, file_path, caption)
        elif media_type == "video":
            success = whatsapp_client.send_video(customer.phone, file_path, caption)
        elif media_type == "document":
            success = whatsapp_client.send_document(customer.phone, file_path, file.filename)
        elif media_type == "audio":
            success = whatsapp_client.send_audio(customer.phone, file_path)
        else:
            raise HTTPException(status_code=400, detail=f"不支持的媒体类型: {media_type}")
        
        if success:
            # 记录到数据库
            message = Message(
                customer_id=customer_id,
                direction="outgoing",
                content=f"[{media_type}] {caption}" if caption else f"[{media_type}]",
                sender_name="系统",
                is_read=True
            )
            db.add(message)
            db.commit()
            
            return {"success": True, "message": f"{media_type} 已发送"}
        else:
            raise HTTPException(status_code=500, detail="发送失败")
            
    finally:
        # 清理临时文件
        if os.path.exists(file_path):
            os.remove(file_path)


@app.get("/api/whatsapp/user-info/{phone}")
async def get_whatsapp_user_info(phone: str):
    """查询 WhatsApp 用户信息（是否注册）"""
    if not whatsapp_client or not whatsapp_client.is_authenticated():
        raise HTTPException(status_code=400, detail="WhatsApp 未登录")
    
    # 清理手机号格式
    clean_phone = phone.replace("+", "").replace(" ", "").replace("-", "")
    
    info = whatsapp_client.get_user_info(clean_phone)
    if info:
        return {"success": True, "info": info}
    else:
        raise HTTPException(status_code=500, detail="查询失败")


class StatusUpdateRequest(BaseModel):
    status: str


class ProfileNameRequest(BaseModel):
    name: str


@app.post("/api/whatsapp/status")
async def set_whatsapp_status(request: StatusUpdateRequest):
    """设置 WhatsApp 状态消息"""
    if not whatsapp_client or not whatsapp_client.is_authenticated():
        raise HTTPException(status_code=400, detail="WhatsApp 未登录")
    
    success = whatsapp_client.set_status_message(request.status)
    if success:
        return {"success": True, "message": "状态已更新"}
    else:
        raise HTTPException(status_code=500, detail="设置状态失败")


@app.get("/api/whatsapp/status")
async def get_whatsapp_status():
    """获取 WhatsApp 状态消息"""
    if not whatsapp_client or not whatsapp_client.is_authenticated():
        raise HTTPException(status_code=400, detail="WhatsApp 未登录")
    
    status = whatsapp_client.get_status_message()
    return {"success": True, "status": status}


@app.post("/api/whatsapp/profile-name")
async def set_whatsapp_profile_name(request: ProfileNameRequest):
    """设置 WhatsApp 显示名称"""
    if not whatsapp_client or not whatsapp_client.is_authenticated():
        raise HTTPException(status_code=400, detail="WhatsApp 未登录")
    
    success = whatsapp_client.set_profile_name(request.name)
    if success:
        return {"success": True, "message": "名称已更新"}
    else:
        raise HTTPException(status_code=500, detail="设置名称失败")


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
    # 如果文档有关联文件，一并删除
    docs = kb.get_all_documents()
    doc = next((d for d in docs if d["id"] == doc_id), None)
    if doc and doc.get("file_path") and os.path.exists(doc["file_path"]):
        try:
            os.remove(doc["file_path"])
        except Exception:
            pass
    success = kb.delete_document(doc_id)
    
    if success:
        return {"success": True, "message": "文档已删除"}
    else:
        raise HTTPException(status_code=500, detail="删除文档失败")


class DocumentUpdateRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None


@app.get("/api/knowledge/documents/{doc_id}")
async def get_document(doc_id: int):
    """获取单个文档详情（含 content 和附件列表）"""
    kb = get_knowledge_base()
    doc = kb.get_document_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    
    # 获取附件列表
    attachments = kb.get_attachments(doc_id)
    doc['attachments'] = attachments
    
    return {"success": True, "document": doc}


@app.put("/api/knowledge/documents/{doc_id}")
async def update_document(doc_id: int, request: DocumentUpdateRequest):
    """编辑知识库文档（仅支持文字类型）"""
    kb = get_knowledge_base()
    doc = kb.get_document_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    success = kb.update_document(
        doc_id,
        title=request.title,
        content=request.content,
        category=request.category
    )
    if success:
        return {"success": True, "message": "文档已更新"}
    else:
        raise HTTPException(status_code=500, detail="更新失败")


@app.post("/api/knowledge/upload")
async def upload_knowledge_file(
    file: UploadFile = File(...),
    title: str = Form(""),
    category: str = Form("general"),
    replace_doc_id: Optional[int] = None
):
    """上传文件到知识库（图片/PDF/Word/文本）
    
    参数 replace_doc_id: 如果提供，则替换指定ID的文档（用于编辑文件类文档）
    """
    from file_parser import extract_text_from_file, get_doc_type, IMAGE_EXTENSIONS
    import uuid
    
    # 限制文件大小：50MB
    MAX_SIZE = 50 * 1024 * 1024
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="文件过大，最大支持 50MB")
    
    # 检查文件类型
    allowed_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".docx", ".doc", ".txt", ".md", ".csv"}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}")
    
    # 生成唯一文件名，保存到本地
    safe_name = f"{uuid.uuid4().hex}{ext}"
    kb_files_dir = os.path.join(os.path.dirname(__file__), "data", "knowledge_files")
    os.makedirs(kb_files_dir, exist_ok=True)
    file_path = os.path.join(kb_files_dir, safe_name)
    
    # 验证保存路径不超出目标目录（防止路径遍历攻击）
    abs_path = os.path.abspath(file_path)
    abs_dir = os.path.abspath(kb_files_dir)
    if not abs_path.startswith(abs_dir):
        raise HTTPException(status_code=400, detail="无效的文件路径")
    
    with open(file_path, "wb") as f:
        f.write(content)
    
    # 构建可访问 URL
    file_url = f"/knowledge-files/{safe_name}"
    
    # 提取文字内容
    extracted_text = extract_text_from_file(file_path, file.filename)
    doc_type = get_doc_type(file.filename)
    
    # 标题：优先用用户输入，否则用文件名
    doc_title = title.strip() if title.strip() else os.path.splitext(file.filename)[0]
    
    # 图片类型：内容为路径描述；文档类型：使用提取内容
    if doc_type == "image":
        kb_content = f"[产品图片] {doc_title}。图片链接：{file_url}"
    else:
        kb_content = extracted_text if extracted_text else f"[{doc_type}文件] {doc_title}"
    
    # AI 智能分析：自动分类、生成摘要和关键词
    ai_analysis = {"category": category, "summary": "", "keywords": []}
    ai_error = None
    if kb_content and len(kb_content) > 20:
        try:
            from llm_service import get_llm_service
            llm = get_llm_service()
            ai_analysis = llm.analyze_document(kb_content)
            print(f"[知识库AI分析] 文档: {doc_title}, 分类: {ai_analysis['category']}, 关键词: {ai_analysis['keywords']}")
        except PermissionError as e:
            ai_error = str(e)
            print(f"[知识库AI分析] 认证失败: {e}")
        except Exception as e:
            print(f"[知识库AI分析] 分析失败，使用默认分类: {e}")
    
    # 使用 AI 分析结果优化内容
    ai_category = ai_analysis.get("category", category)
    ai_summary = ai_analysis.get("summary", "")
    ai_keywords = ai_analysis.get("keywords", [])
    
    # 将 AI 摘要和关键词追加到内容中，增强可检索性
    enriched_content = kb_content
    if ai_summary:
        enriched_content += f"\n\n[AI摘要] {ai_summary}"
    if ai_keywords:
        enriched_content += f"\n[关键词] {', '.join(ai_keywords)}"
    
    kb = get_knowledge_base()
    
    # 如果是替换模式
    if replace_doc_id:
        # 获取原文档信息，删除旧文件
        old_doc = kb.get_document_by_id(replace_doc_id)
        if old_doc and old_doc.get('file_path') and os.path.exists(old_doc['file_path']):
            try:
                os.remove(old_doc['file_path'])
            except:
                pass
        
        # 更新文档
        success = kb.update_document(
            doc_id=replace_doc_id,
            title=doc_title,
            content=enriched_content,
            category=ai_category,
            file_path=file_path,
            file_url=file_url,
            file_type=doc_type
        )
        
        if not success:
            os.remove(file_path)
            raise HTTPException(status_code=500, detail="更新文档失败")
        
        result = {
            "success": True,
            "message": "文档已更新",
            "id": replace_doc_id,
            "title": doc_title,
            "doc_type": doc_type,
            "category": ai_category,
            "file_url": file_url,
            "ai_summary": ai_summary,
            "ai_keywords": ai_keywords,
            "content_preview": enriched_content[:200]
        }
        if ai_error:
            result["warning"] = ai_error
        return result
    else:
        # 新增模式
        doc_id = kb.add_file_document(
            title=doc_title,
            file_path=file_path,
            file_url=file_url,
            content=enriched_content,
            doc_type=doc_type,
            category=ai_category
        )
        
        if not doc_id:
            os.remove(file_path)
            raise HTTPException(status_code=500, detail="保存到知识库失败")
        
        result = {
            "success": True,
            "message": "文件上传成功",
            "id": doc_id,
            "title": doc_title,
            "doc_type": doc_type,
            "category": ai_category,
            "file_url": file_url,
            "ai_summary": ai_summary,
            "ai_keywords": ai_keywords,
            "content_preview": enriched_content[:200]
        }
        if ai_error:
            result["warning"] = ai_error
        return result


@app.get("/api/knowledge/search")
async def search_knowledge(q: str):
    """搜索知识库"""
    kb = get_knowledge_base()
    results = kb.search_documents(q)
    return {"success": True, "results": results}


class AnalyzeTextRequest(BaseModel):
    title: str = ""
    content: str


@app.post("/api/knowledge/analyze-text")
async def analyze_text_to_knowledge(request: AnalyzeTextRequest):
    """将文本内容通过AI分析后保存到知识库"""
    content = request.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="内容不能为空")
    
    # AI 智能分析
    ai_analysis = {"category": "general", "summary": "", "keywords": []}
    try:
        from llm_service import get_llm_service
        llm = get_llm_service()
        ai_analysis = llm.analyze_document(content)
        print(f"[知识库AI分析] 分类: {ai_analysis['category']}, 关键词: {ai_analysis['keywords']}")
    except PermissionError as e:
        # API Key 认证失败，直接返回错误
        print(f"[知识库AI分析] 认证失败: {e}")
        raise HTTPException(status_code=401, detail=str(e))
    except ValueError as e:
        # 未配置 API Key
        print(f"[知识库AI分析] 配置错误: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[知识库AI分析] 分析失败，使用默认分类: {e}")
    
    ai_category = ai_analysis.get("category", "general")
    ai_summary = ai_analysis.get("summary", "")
    ai_keywords = ai_analysis.get("keywords", [])
    
    # 自动生成标题（如果未提供）
    doc_title = request.title.strip()
    if not doc_title:
        if ai_summary:
            doc_title = ai_summary[:30] + "..." if len(ai_summary) > 30 else ai_summary
        else:
            doc_title = content[:30] + "..." if len(content) > 30 else content
    
    # 将 AI 摘要和关键词追加到内容中
    enriched_content = content
    if ai_summary:
        enriched_content += f"\n\n[AI摘要] {ai_summary}"
    if ai_keywords:
        enriched_content += f"\n[关键词] {', '.join(ai_keywords)}"
    
    # 保存到知识库
    kb = get_knowledge_base()
    doc_id = kb.add_document(
        title=doc_title,
        content=enriched_content,
        category=ai_category,
        doc_type="text"
    )
    
    if not doc_id:
        raise HTTPException(status_code=500, detail="保存到知识库失败")
    
    return {
        "success": True,
        "message": "文档已保存",
        "id": doc_id,
        "title": doc_title,
        "category": ai_category,
        "ai_summary": ai_summary,
        "ai_keywords": ai_keywords
    }


# ============ 知识库附件 API ============

@app.get("/api/knowledge/documents/{doc_id}/attachments")
async def get_document_attachments(doc_id: int):
    """获取文档的所有附件"""
    kb = get_knowledge_base()
    doc = kb.get_document_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    attachments = kb.get_attachments(doc_id)
    return {"success": True, "attachments": attachments}


@app.post("/api/knowledge/documents/{doc_id}/attachments")
async def add_document_attachment(
    doc_id: int, 
    file: UploadFile = File(...),
    name: str = Form("")
):
    """为文档添加附件（图片/PDF等）
    
    Args:
        name: 附件名称/描述（用于AI识别），默认使用文件名
    """
    import uuid
    
    kb = get_knowledge_base()
    doc = kb.get_document_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    
    # 限制文件大小：50MB
    MAX_SIZE = 50 * 1024 * 1024
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="文件过大，最大支持 50MB")
    
    # 检查文件类型
    allowed_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".docx", ".doc", ".txt", ".md", ".csv"}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}")
    
    # 确定文件类型
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        file_type = "image"
    elif ext == ".pdf":
        file_type = "pdf"
    elif ext in {".docx", ".doc"}:
        file_type = "document"
    else:
        file_type = "text"
    
    # 生成唯一文件名，保存到本地
    safe_name = f"{uuid.uuid4().hex}{ext}"
    kb_files_dir = os.path.join(os.path.dirname(__file__), "data", "knowledge_files")
    os.makedirs(kb_files_dir, exist_ok=True)
    file_path = os.path.join(kb_files_dir, safe_name)
    
    with open(file_path, "wb") as f:
        f.write(content)
    
    # 构建可访问 URL
    file_url = f"/knowledge-files/{safe_name}"
    
    # 使用传入的 name 或默认使用文件名（不含扩展名）
    attachment_name = name.strip() if name.strip() else os.path.splitext(file.filename)[0]
    
    # 添加到附件表
    attachment_id = kb.add_attachment(
        doc_id=doc_id,
        name=attachment_name,
        file_name=file.filename,
        file_path=file_path,
        file_url=file_url,
        file_type=file_type
    )
    
    return {
        "success": True,
        "message": "附件添加成功",
        "attachment": {
            "id": attachment_id,
            "name": attachment_name,
            "file_name": file.filename,
            "file_url": file_url,
            "file_type": file_type
        }
    }


@app.put("/api/knowledge/attachments/{attachment_id}/name")
async def update_attachment_name(attachment_id: int, name: str = Body(..., embed=True)):
    """更新附件名称"""
    kb = get_knowledge_base()
    
    # 获取附件信息
    attachment = kb.get_attachment_by_id(attachment_id)
    if not attachment:
        raise HTTPException(status_code=404, detail="附件不存在")
    
    # 更新名称
    success = kb.update_attachment_name(attachment_id, name)
    if success:
        return {"success": True, "message": "附件名称已更新"}
    else:
        raise HTTPException(status_code=500, detail="更新失败")


@app.delete("/api/knowledge/attachments/{attachment_id}")
async def delete_document_attachment(attachment_id: int):
    """删除附件"""
    kb = get_knowledge_base()
    
    # 获取附件信息
    attachment = kb.get_attachment_by_id(attachment_id)
    if not attachment:
        raise HTTPException(status_code=404, detail="附件不存在")
    
    # 删除物理文件
    if os.path.exists(attachment['file_path']):
        try:
            os.remove(attachment['file_path'])
        except Exception as e:
            print(f"删除附件文件失败: {e}")
    
    # 删除数据库记录
    success = kb.delete_attachment(attachment_id)
    if success:
        return {"success": True, "message": "附件已删除"}
    else:
        raise HTTPException(status_code=500, detail="删除附件失败")


# ============ AI 记忆 API ============

@app.get("/api/ai-memory/summary")
async def get_ai_memory_summary():
    """获取 AI 记忆摘要（后台展示）"""
    from ai_memory import get_all_memories
    memories = get_all_memories()
    return {"success": True, "memories": memories}


@app.post("/api/ai-memory/trigger/{customer_id}")
async def trigger_memory_summarize(customer_id: int, db: Session = Depends(get_db)):
    """手动触发指定客户的对话总结"""
    from ai_memory import summarize_conversation
    
    # 获取客户最近消息
    msgs = db.query(Message).filter(
        Message.customer_id == customer_id
    ).order_by(Message.created_at.asc()).limit(40).all()
    
    if not msgs:
        raise HTTPException(status_code=404, detail="没有找到对话记录")
    
    messages = [
        {"role": "user" if m.direction == "incoming" else "assistant", "content": m.content}
        for m in msgs
    ]
    
    result = await summarize_conversation(customer_id, messages)
    if result:
        return {"success": True, "summary": result}
    else:
        raise HTTPException(status_code=500, detail="总结失败，请检查日志")


@app.delete("/api/ai-memory/entry")
async def delete_memory_entry(memory_type: str, index: int):
    """删除单条记忆"""
    from ai_memory import delete_memory_entry as _delete
    success = _delete(memory_type, index)
    if success:
        return {"success": True, "message": "已删除"}
    raise HTTPException(status_code=404, detail="记忆条目不存在")


@app.delete("/api/ai-memory/clear")
async def clear_ai_memory():
    """清空所有 AI 记忆（重置训练）"""
    from ai_memory import clear_all_memories
    clear_all_memories()
    return {"success": True, "message": "AI 记忆已清空"}


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


@app.put("/api/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: int,
    name: str = Body(...),
    message_template: str = Body(...),
    target_tags: List[int] = Body([]),
    schedule_time: str = Body(...),
    interval_seconds: int = Body(60)
):
    """更新发送计划（pending 和 completed 状态可编辑）"""
    try:
        scheduler = get_scheduler_service()
        success = scheduler.update_schedule(
            schedule_id=schedule_id,
            name=name,
            message_template=message_template,
            target_tags=target_tags,
            schedule_time=schedule_time,
            interval_seconds=interval_seconds
        )
        
        if success:
            return {"success": True, "message": "计划已更新"}
        else:
            raise HTTPException(status_code=404, detail="计划不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新失败: {str(e)}")


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
    knowledge_doc_ids: List[int] = []  # 绑定的知识库文档ID列表
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
    knowledge_doc_ids: Optional[List[int]] = None  # 绑定的知识库文档ID列表
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    priority: Optional[int] = None

@app.get("/api/agents")
async def get_agents(db: Session = Depends(get_db)):
    """获取所有AI智能体"""
    agents = db.query(AIAgent).all()
    result = []
    # 获取知识库文档列表（用于返回文档标题）
    kb = get_knowledge_base()
    kb_docs_map = {doc['id']: doc for doc in kb.get_documents()}
    
    for agent in agents:
        # 获取绑定的标签
        tag_bindings = db.query(AgentTagBinding).filter(AgentTagBinding.agent_id == agent.id).all()
        tag_ids = [b.tag_id for b in tag_bindings]
        tags = db.query(CustomerTag).filter(CustomerTag.id.in_(tag_ids)).all() if tag_ids else []
        
        # 获取绑定的知识库文档
        knowledge_bindings = db.query(AgentKnowledgeBinding).filter(AgentKnowledgeBinding.agent_id == agent.id).all()
        knowledge_doc_ids = [b.knowledge_doc_id for b in knowledge_bindings]
        knowledge_docs = []
        for doc_id in knowledge_doc_ids:
            doc = kb_docs_map.get(doc_id)
            if doc:
                knowledge_docs.append({'id': doc_id, 'title': doc.get('title', f'文档#{doc_id}')})
            else:
                knowledge_docs.append({'id': doc_id, 'title': f'文档#{doc_id}'})
        
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
            "knowledge_docs": knowledge_docs,
            "knowledge_doc_ids": knowledge_doc_ids,
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
    
    # 绑定知识库文档
    for doc_id in agent_data.knowledge_doc_ids:
        db.add(AgentKnowledgeBinding(agent_id=agent.id, knowledge_doc_id=doc_id))
    
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
    
    if "knowledge_doc_ids" in update_data:
        knowledge_doc_ids = update_data.pop("knowledge_doc_ids")
        # 删除旧知识库绑定
        db.query(AgentKnowledgeBinding).filter(AgentKnowledgeBinding.agent_id == agent_id).delete()
        # 添加新绑定
        for doc_id in knowledge_doc_ids:
            db.add(AgentKnowledgeBinding(agent_id=agent_id, knowledge_doc_id=doc_id))
    
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
    
    # 调用 LLM API（带重试逻辑）
    import httpx
    import asyncio
    max_retries = 2
    last_error = None
    
    for attempt in range(max_retries + 1):
        try:
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
                        "max_tokens": 1500
                    },
                    timeout=60.0  # 增加超时时间到60秒
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
                last_error = f"{response.status_code} - {response.text}"
                print(f"[PromptOptimize] 尝试 {attempt+1}/{max_retries+1} 失败: {last_error}")
                if attempt < max_retries:
                    await asyncio.sleep(2)
                continue
        
        except httpx.ReadTimeout as e:
            last_error = "ReadTimeout"
            print(f"[PromptOptimize] 尝试 {attempt+1}/{max_retries+1} 超时，正在进行第 {attempt+1} 次重试...")
            if attempt < max_retries:
                await asyncio.sleep(2)
            continue
        except Exception as e:
            last_error = str(e)
            print(f"[PromptOptimize] 尝试 {attempt+1}/{max_retries+1} 失败: {last_error}")
            if attempt < max_retries:
                await asyncio.sleep(2)
            continue
    
    # 所有重试都失败
    return {"success": False, "message": f"优化失败: {last_error}"}


# ============ 自动打标签规则 API ============

class AutoTagRuleCreate(BaseModel):
    name: str
    tag_id: int
    condition_type: str  # new_customer, follow_up, old_customer, quote_requested, keyword_match, message_received
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


# ================================================================
# 管理员通知 API
# ================================================================

class AdminCreate(BaseModel):
    name: str
    phone: str

class AdminUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    enabled: Optional[bool] = None

class RuleUpdate(BaseModel):
    enabled: Optional[bool] = None
    keywords: Optional[List[str]] = None
    name: Optional[str] = None
    description: Optional[str] = None
    report_time: Optional[str] = None

class CustomRuleCreate(BaseModel):
    rule_id: str
    name: str
    description: str = ""
    keywords: List[str]


@app.get("/api/notify/admins")
async def list_admins():
    """获取管理员列表"""
    db = get_admin_notify_db()
    return {"success": True, "admins": db.get_admins()}


@app.post("/api/notify/admins")
async def create_admin(data: AdminCreate):
    """添加管理员"""
    db = get_admin_notify_db()
    admin_id = db.add_admin(data.name, data.phone)
    return {"success": True, "message": "管理员已添加", "id": admin_id}


@app.put("/api/notify/admins/{admin_id}")
async def update_admin(admin_id: int, data: AdminUpdate):
    """更新管理员"""
    db = get_admin_notify_db()
    db.update_admin(admin_id, name=data.name, phone=data.phone, enabled=data.enabled)
    return {"success": True, "message": "管理员已更新"}


@app.delete("/api/notify/admins/{admin_id}")
async def delete_admin(admin_id: int):
    """删除管理员"""
    db = get_admin_notify_db()
    db.delete_admin(admin_id)
    return {"success": True, "message": "管理员已删除"}


@app.get("/api/notify/rules")
async def list_notify_rules():
    """获取通知规则列表"""
    db = get_admin_notify_db()
    return {"success": True, "rules": db.get_rules()}


@app.put("/api/notify/rules/{rule_id}")
async def update_notify_rule(rule_id: str, data: RuleUpdate):
    """更新规则开关/关键词"""
    db = get_admin_notify_db()
    ok = db.update_rule(
        rule_id,
        enabled=data.enabled,
        keywords=data.keywords,
        name=data.name,
        description=data.description,
        report_time=data.report_time
    )
    if ok:
        return {"success": True, "message": "规则已更新"}
    raise HTTPException(status_code=404, detail="规则不存在")


@app.post("/api/notify/rules")
async def create_custom_rule(data: CustomRuleCreate):
    """新建自定义关键词规则"""
    db = get_admin_notify_db()
    db.add_custom_rule(data.rule_id, data.name, data.description, data.keywords)
    return {"success": True, "message": "自定义规则已添加"}


@app.delete("/api/notify/rules/{rule_id}")
async def delete_custom_rule(rule_id: str):
    """删除自定义规则"""
    db = get_admin_notify_db()
    ok = db.delete_custom_rule(rule_id)
    if ok:
        return {"success": True, "message": "规则已删除"}
    raise HTTPException(status_code=400, detail="内置规则不能删除")


@app.post("/api/notify/test")
async def test_notify():
    """向所有管理员发送测试消息"""
    if not whatsapp_client or not whatsapp_client.is_authenticated():
        return {"success": False, "message": "WhatsApp 未登录"}
    db = get_admin_notify_db()
    phones = db.get_active_admin_phones()
    if not phones:
        return {"success": False, "message": "暂无启用的管理员"}
    svc = get_notify_service()
    svc.set_client(whatsapp_client)
    from datetime import datetime as _dt
    test_text = f"🔔 *管理员通知测试*\n\n测试消息发送成功！\n时间：{_dt.now().strftime('%Y-%m-%d %H:%M:%S')}"
    sent = []
    for phone in phones:
        try:
            whatsapp_client.send_message(phone, test_text)
            sent.append(phone)
        except Exception as e:
            pass
    return {"success": True, "message": f"已向 {len(sent)} 个管理员发送测试消息", "sent": sent}


@app.post("/api/notify/trigger-daily-report")
async def trigger_daily_report():
    """手动触发今日报告"""
    if not whatsapp_client or not whatsapp_client.is_authenticated():
        return {"success": False, "message": "WhatsApp 未登录"}
    svc = get_notify_service()
    svc.set_client(whatsapp_client)
    svc.send_daily_report()
    return {"success": True, "message": "日报已发送"}


# 日报定时任务（线程轮询）
def _start_daily_report_scheduler():
    """.在后台线程中每分钟检查是否到达日报时间"""
    import threading, time
    _last_sent_date = [None]

    def _loop():
        while True:
            try:
                now = datetime.now()
                db = get_admin_notify_db()
                rule = db.get_rule("daily_report")
                if rule and rule.get("enabled"):
                    report_time = rule.get("report_time", "23:59")
                    h, m = map(int, report_time.split(":"))
                    if now.hour == h and now.minute == m:
                        today = now.date()
                        if _last_sent_date[0] != today:
                            _last_sent_date[0] = today
                            if whatsapp_client and whatsapp_client.is_authenticated():
                                svc = get_notify_service()
                                svc.set_client(whatsapp_client)
                                svc.send_daily_report()
            except Exception as e:
                pass
            time.sleep(60)  # 每分钟检查一次

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


# 服务启动时启动日报调度
_start_daily_report_scheduler()


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", 8000)),
        reload=os.getenv("DEBUG", "false").lower() == "true"
    )
