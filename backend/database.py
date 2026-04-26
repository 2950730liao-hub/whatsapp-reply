"""
数据库配置和模型定义
"""
from sqlalchemy import create_engine, Column, Integer, String, Text, Boolean, DateTime, ForeignKey, JSON, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import os

# 数据库配置 - 使用绝对路径确保一致性
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'data', 'whatsapp_crm.db')}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    echo=False,
    # 连接池配置
    pool_size=5,            # 连接池大小
    max_overflow=5,         # 超出pool_size时最多可创建的连接数
    pool_recycle=3600,      # 连接回收时间（秒），防止连接超时
    pool_pre_ping=True      # 连接前ping测试，自动回收失效连接
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Customer(Base):
    """客户表"""
    __tablename__ = "customers"
    
    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String(20), unique=True, index=True, nullable=False)
    name = Column(String(100))
    category = Column(String(20), default="new")  # new/lead/returning
    status = Column(String(20), default="active")  # active/pending/closed
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    messages = relationship("Message", back_populates="customer", cascade="all, delete-orphan")
    conversations = relationship("Conversation", back_populates="customer", cascade="all, delete-orphan")
    tags = relationship("CustomerTagAssociation", back_populates="customer", cascade="all, delete-orphan")


class Message(Base):
    """消息表"""
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    wa_message_id = Column(String(100), unique=True, index=True)
    content = Column(Text)
    direction = Column(String(10))  # incoming/outgoing
    sender_name = Column(String(100))
    message_type = Column(String(20), default="text")
    is_read = Column(Boolean, default=False)
    is_processed = Column(Boolean, default=False)  # AI 是否已处理（回复）
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    customer = relationship("Customer", back_populates="messages")


class Conversation(Base):
    """会话表"""
    __tablename__ = "conversations"
    
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    status = Column(String(20), default="bot")  # bot/handover/closed
    assigned_agent_id = Column(Integer, ForeignKey("agents.id"), index=True)
    last_message_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    customer = relationship("Customer", back_populates="conversations")
    agent = relationship("Agent", back_populates="conversations")


class Agent(Base):
    """销售员/客服表"""
    __tablename__ = "agents"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(100), unique=True, index=True)
    phone = Column(String(20))  # 用于手机通知
    password_hash = Column(String(255))
    web_push_subscription = Column(JSON)  # Web Push 订阅信息
    is_online = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    conversations = relationship("Conversation", back_populates="agent")


class CommunicationPlan(Base):
    """沟通计划表"""
    __tablename__ = "communication_plans"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    category = Column(String(20))  # 适用客户分类
    trigger_type = Column(String(20))  # immediate/delay/schedule
    trigger_delay_minutes = Column(Integer, default=0)
    message_template = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    executions = relationship("PlanExecution", back_populates="plan", cascade="all, delete-orphan")


class PlanExecution(Base):
    """计划执行记录"""
    __tablename__ = "plan_executions"
    
    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("communication_plans.id"), nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    status = Column(String(20), default="pending")  # pending/sent/failed
    scheduled_at = Column(DateTime)
    executed_at = Column(DateTime)
    error_message = Column(Text)
    
    # 关系
    plan = relationship("CommunicationPlan", back_populates="executions")


class CustomerTag(Base):
    """客户标签表 - 支持自定义标签"""
    __tablename__ = "customer_tags"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)
    color = Column(String(7), default="#007bff")  # 标签颜色
    description = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    customers = relationship("CustomerTagAssociation", back_populates="tag")


class CustomerTagAssociation(Base):
    """客户-标签关联表"""
    __tablename__ = "customer_tag_associations"
    
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    tag_id = Column(Integer, ForeignKey("customer_tags.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    customer = relationship("Customer", back_populates="tags")
    tag = relationship("CustomerTag", back_populates="customers")


class AIAgent(Base):
    """AI智能体表 - 支持多智能体配置"""
    __tablename__ = "ai_agents"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)  # 智能体名称
    description = Column(Text)  # 智能体描述
    system_prompt = Column(Text, nullable=False)  # 系统提示词
    
    # 大模型配置（可选，如果为空则使用默认模型）
    llm_provider_id = Column(Integer, ForeignKey("llm_providers.id"), nullable=True)
    llm_model_id = Column(String(100))  # 指定使用哪个模型ID
    
    # 智能体级别的参数（覆盖模型默认参数）
    temperature = Column(Float)  # 温度参数（可选）
    max_tokens = Column(Integer)  # 最大token数（可选）
    
    is_active = Column(Boolean, default=True)  # 是否启用
    is_default = Column(Boolean, default=False)  # 是否为默认智能体
    priority = Column(Integer, default=0)  # 优先级（数字越大优先级越高）
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    tag_bindings = relationship("AgentTagBinding", back_populates="agent", cascade="all, delete-orphan")
    knowledge_bindings = relationship("AgentKnowledgeBinding", back_populates="agent", cascade="all, delete-orphan")
    llm_provider = relationship("LLMProvider")


class AgentTagBinding(Base):
    """智能体-客户标签绑定表"""
    __tablename__ = "agent_tag_bindings"
    
    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("ai_agents.id"), nullable=False, index=True)
    tag_id = Column(Integer, ForeignKey("customer_tags.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    agent = relationship("AIAgent", back_populates="tag_bindings")
    tag = relationship("CustomerTag")


class AgentKnowledgeBinding(Base):
    """智能体-知识库绑定表"""
    __tablename__ = "agent_knowledge_bindings"
    
    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("ai_agents.id"), nullable=False, index=True)
    knowledge_doc_id = Column(Integer, nullable=False, index=True)  # 关联到知识库文档ID
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    agent = relationship("AIAgent", back_populates="knowledge_bindings")


class LLMProvider(Base):
    """大模型提供商配置表"""
    __tablename__ = "llm_providers"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)  # 显示名称，如"DeepSeek官方"
    provider_type = Column(String(50), nullable=False)  # deepseek/openai/claude等
    api_key = Column(Text, nullable=False)  # API密钥
    base_url = Column(String(500))  # API基础URL
    default_model = Column(String(100), nullable=False)  # 默认模型，如"deepseek-chat"
    is_active = Column(Boolean, default=True)  # 是否启用
    is_default = Column(Boolean, default=False)  # 是否为默认提供商
    temperature = Column(Float, default=0.7)
    max_tokens = Column(Integer, default=500)
    timeout = Column(Integer, default=30)  # 请求超时时间（秒）
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LLMModel(Base):
    """大模型配置表 - 每个提供商下的具体模型"""
    __tablename__ = "llm_models"
    
    id = Column(Integer, primary_key=True, index=True)
    provider_id = Column(Integer, ForeignKey("llm_providers.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)  # 显示名称
    model_id = Column(String(100), nullable=False)  # API调用用的模型ID
    is_active = Column(Boolean, default=True)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关系
    provider = relationship("LLMProvider")





def init_db():
    """初始化数据库（自动迁移列）"""
    import saas.models
    Base.metadata.create_all(bind=engine)
    
    # 自动迁移：检查 messages 表是否有 is_processed 列
    from sqlalchemy import text
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA table_info(messages)"))
        columns = [row[1] for row in result]
        if "is_processed" not in columns:
            conn.execute(text("ALTER TABLE messages ADD COLUMN is_processed BOOLEAN DEFAULT 0"))
            conn.commit()
            print("[DB Migrate] 已添加 messages.is_processed 列")


class AutoTagRule(Base):
    """自动打标签规则表"""
    __tablename__ = "auto_tag_rules"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)  # 规则名称
    tag_id = Column(Integer, ForeignKey("customer_tags.id", ondelete="CASCADE"), nullable=False, index=True)  # 要打的标签
    condition_type = Column(String(50), nullable=False)  # 条件类型：message_received/quote_requested/keyword_match/ai_detected
    condition_config = Column(JSON, default={})  # 条件配置（如关键词列表等）
    is_active = Column(Boolean, default=True)  # 是否启用
    priority = Column(Integer, default=0)  # 优先级
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    tag = relationship("CustomerTag")


class CustomerTagLog(Base):
    """客户标签操作日志表"""
    __tablename__ = "customer_tag_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    tag_id = Column(Integer, ForeignKey("customer_tags.id"), nullable=False, index=True)
    action = Column(String(20), nullable=False)  # add/remove
    source = Column(String(50))  # 来源：manual/auto_rule/rule_id
    source_id = Column(Integer, index=True)  # 如果是自动规则，记录规则ID
    created_at = Column(DateTime, default=datetime.utcnow)


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
