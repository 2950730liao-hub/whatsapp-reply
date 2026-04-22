"""
大模型服务 - 集成OpenAI/Claude实现智能回复
"""
import os
import time
import asyncio
import logging
import httpx
from typing import Optional, List, Dict, Union, Any
from database import Customer, Message, AIAgent, CustomerTagAssociation, AgentTagBinding, LLMProvider
from config_service import get_config_service

logger = logging.getLogger(__name__)


def _get_customer_attr(customer: Union[Customer, Dict[str, Any]], attr: str, default=None):
    """
    统一获取客户属性，支持 ORM 对象和 dict 类型
    
    Args:
        customer: Customer ORM 对象或 dict
        attr: 属性名
        default: 默认值
        
    Returns:
        属性值
    """
    if isinstance(customer, dict):
        return customer.get(attr, default)
    return getattr(customer, attr, default)


class LLMService:
    """大模型服务"""
    
    CONFIG_REFRESH_INTERVAL = 60  # 配置刷新间隔（秒）
    
    def __init__(self):
        self.config = get_config_service()
        self._config_last_refreshed = 0
        self._refresh_config()
    
    def _refresh_config(self):
        """刷新配置（优先使用数据库 Provider 配置，兼容旧配置）"""
        # 优先从数据库获取默认 Provider
        try:
            from database import SessionLocal, LLMProvider
            db = SessionLocal()
            try:
                provider = db.query(LLMProvider).filter(
                    LLMProvider.is_default == True,
                    LLMProvider.is_active == True
                ).first()
                if provider:
                    self.api_key = provider.api_key
                    self.base_url = provider.base_url or "https://api.openai.com/v1"
                    self.model = provider.default_model or "gpt-3.5-turbo"
                    self._config_last_refreshed = time.time()
                    print(f"[LLMService] 已加载 Provider 配置: {provider.name} ({provider.provider_type}), 模型: {self.model}")
                    return
            finally:
                db.close()
        except Exception as e:
            print(f"[LLMService] 加载 Provider 配置失败，回退到旧配置: {e}")
        
        # 回退到旧版配置
        llm_config = self.config.get_llm_config()
        self.api_key = llm_config.get("api_key") or os.getenv("OPENAI_API_KEY", "")
        self.base_url = llm_config.get("base_url") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.model = llm_config.get("model") or os.getenv("LLM_MODEL", "gpt-3.5-turbo")
        self._config_last_refreshed = time.time()
    
    def _maybe_refresh_config(self):
        """检查并刷新配置（如果已过期）"""
        if time.time() - self._config_last_refreshed > self.CONFIG_REFRESH_INTERVAL:
            print(f"[LLMService] 配置已过期，自动刷新配置...")
            self._refresh_config()
    
    def get_llm_provider_for_agent(self, agent: Optional[AIAgent], db) -> Optional[LLMProvider]:
        """获取智能体使用的大模型提供商"""
        if agent and agent.llm_provider_id:
            provider = db.query(LLMProvider).filter(
                LLMProvider.id == agent.llm_provider_id,
                LLMProvider.is_active == True
            ).first()
            if provider:
                return provider
        
        # 返回默认提供商
        return db.query(LLMProvider).filter(
            LLMProvider.is_default == True,
            LLMProvider.is_active == True
        ).first()
    
    def get_model_for_agent(self, agent: Optional[AIAgent], provider: Optional[LLMProvider]) -> str:
        """获取智能体使用的模型ID"""
        # 优先使用智能体指定的模型
        if agent and agent.llm_model_id:
            return agent.llm_model_id
        # 其次使用提供商的默认模型
        if provider and provider.default_model:
            return provider.default_model
        # 最后使用系统默认
        return self.model
    
    def get_agent_for_customer(self, customer: Customer, db) -> Optional[AIAgent]:
        """
        根据客户标签获取匹配的智能体
        
        优先级逻辑：
        1. 优先选择标签精确匹配数量最多的智能体
        2. 如果匹配数量相同，选择优先级（priority）更高的
        3. 如果优先级相同，选择最后更新的（updated_at 最新的）
        """
        # 获取客户的所有标签ID
        if isinstance(customer, dict):
            # 如果 customer 是 dict，从数据库查询标签
            customer_id = customer.get('id')
            customer_tag_ids = set([assoc.tag_id for assoc in db.query(CustomerTagAssociation).filter(
                CustomerTagAssociation.customer_id == customer_id
            ).all()])
        else:
            # ORM 对象直接访问 tags 属性
            customer_tag_ids = set([assoc.tag_id for assoc in customer.tags])
        
        if customer_tag_ids:
            # 查找匹配这些标签的智能体绑定
            agent_bindings = db.query(AgentTagBinding).filter(
                AgentTagBinding.tag_id.in_(customer_tag_ids)
            ).all()
            
            if agent_bindings:
                # 统计每个智能体匹配的标签数量
                agent_match_count = {}
                agent_ids = set()
                for binding in agent_bindings:
                    agent_ids.add(binding.agent_id)
                    if binding.agent_id not in agent_match_count:
                        agent_match_count[binding.agent_id] = 0
                    if binding.tag_id in customer_tag_ids:
                        agent_match_count[binding.agent_id] += 1
                
                # 获取所有匹配的智能体详情
                agents = db.query(AIAgent).filter(
                    AIAgent.id.in_(list(agent_ids)),
                    AIAgent.is_active == True
                ).all()
                
                if agents:
                    # 按匹配标签数量降序、优先级降序、更新时间降序排序
                    sorted_agents = sorted(
                        agents,
                        key=lambda a: (
                            agent_match_count.get(a.id, 0),  # 匹配标签数量（高优先）
                            a.priority or 0,                  # 优先级（高优先）
                            a.updated_at or a.created_at      # 更新时间（新优先）
                        ),
                        reverse=True
                    )
                    
                    selected_agent = sorted_agents[0]
                    match_count = agent_match_count.get(selected_agent.id, 0)
                    customer_display = _get_customer_attr(customer, 'name') or _get_customer_attr(customer, 'phone')
                    print(f"[智能体选择] 客户 '{customer_display}' 选中智能体: '{selected_agent.name}' "
                          f"(匹配标签数: {match_count}, 优先级: {selected_agent.priority or 0}, "
                          f"ID: {selected_agent.id})")
                    return selected_agent
        
        # 如果没有匹配的标签，返回默认智能体
        default_agent = db.query(AIAgent).filter(
            AIAgent.is_default == True,
            AIAgent.is_active == True
        ).first()
        
        if default_agent:
            customer_display = _get_customer_attr(customer, 'name') or _get_customer_attr(customer, 'phone')
            print(f"[智能体选择] 客户 '{customer_display}' 使用默认智能体: '{default_agent.name}'")
            return default_agent
        
        # 如果没有默认智能体，返回第一个启用的智能体
        fallback_agent = db.query(AIAgent).filter(AIAgent.is_active == True).first()
        if fallback_agent:
            customer_display = _get_customer_attr(customer, 'name') or _get_customer_attr(customer, 'phone')
            print(f"[智能体选择] 客户 '{customer_display}' 使用回退智能体: '{fallback_agent.name}'")
        return fallback_agent
    
    async def generate_reply_with_agent(self, customer: Customer, messages: List[Message], 
                                       agent: AIAgent, knowledge_base: Optional[str] = None, db=None) -> str:
        """
        使用指定智能体生成回复
        
        Args:
            customer: 客户信息
            messages: 历史消息列表
            agent: AI智能体配置
            knowledge_base: 知识库内容
            db: 数据库会话
            
        Returns:
            生成的回复文本
        """
        # 检查并刷新配置
        self._maybe_refresh_config()
        
        # 构建对话历史
        conversation_history = []
        for msg in messages[-10:]:  # 最近10条消息
            # 兼容 dict 和 ORM 对象两种形式
            direction = msg.get('direction') if isinstance(msg, dict) else msg.direction
            content = msg.get('content') if isinstance(msg, dict) else msg.content
            role = "user" if direction == "incoming" else "assistant"
            conversation_history.append({
                "role": role,
                "content": content
            })
        
        # 使用智能体的系统提示词
        system_prompt = agent.system_prompt if agent else self._build_system_prompt(customer, knowledge_base)
        
        # 添加知识库内容
        if knowledge_base:
            system_prompt += f"\n\n以下是相关知识库内容，请在回复时参考：\n{knowledge_base}"
        
        # 添加附件使用说明 - AI 主动控制附件发送
        system_prompt += """

【附件发送功能】
当客户询问图片、界面、效果、示例等内容时，知识库中可能包含相关附件（以[附件: 名称 | 路径]格式标记）。
如果你认为需要发送附件给客户，请在回复末尾添加以下标记：
[SEND_ATTACHMENT:附件名称]
例如：[SEND_ATTACHMENT:数字人界面]
系统会自动将该附件发送给客户。"""
        
        # 调用大模型
        messages_for_llm = [
            {"role": "system", "content": system_prompt}
        ] + conversation_history
        
        # 获取大模型提供商和配置
        provider = None
        if db:
            provider = self.get_llm_provider_for_agent(agent, db)
        
        # 确定API配置
        if provider:
            api_key = provider.api_key
            base_url = provider.base_url or "https://api.openai.com/v1"
            model = self.get_model_for_agent(agent, provider)
            # 智能体参数优先，其次提供商参数，最后默认值
            temperature = agent.temperature if agent and agent.temperature is not None else (provider.temperature if provider else 0.7)
            max_tokens = agent.max_tokens if agent and agent.max_tokens is not None else (provider.max_tokens if provider else 500)
            timeout = provider.timeout if provider else 30
            provider_name = provider.name
        else:
            # 使用旧配置作为回退
            api_key = self.api_key
            base_url = self.base_url
            model = self.model
            temperature = agent.temperature if agent and agent.temperature is not None else 0.7
            max_tokens = agent.max_tokens if agent and agent.max_tokens is not None else 500
            timeout = 30
            provider_name = "默认配置"
        
        print(f"[AI回复] 使用模型: {model} (提供商: {provider_name})")
        
        max_retries = 2
        retry_count = 0
        last_error = None
        
        while retry_count <= max_retries:
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
                            "messages": messages_for_llm,
                            "temperature": temperature,
                            "max_tokens": max_tokens
                        },
                        timeout=float(timeout)
                    )
                    
                    if response.status_code == 200:
                        result = response.json()
                        return result["choices"][0]["message"]["content"]
                    elif response.status_code in [429, 502, 503, 504]:  # 可重试的错误码
                        retry_count += 1
                        if retry_count <= max_retries:
                            logger.warning(f"LLM API返回 {response.status_code}，正在进行第 {retry_count} 次重试...")
                            await asyncio.sleep(1)
                            continue
                        else:
                            logger.error(f"LLM API错误: {response.status_code} - {response.text}")
                            return self._get_fallback_reply(customer)
                    else:
                        logger.error(f"LLM API错误: {response.status_code} - {response.text}")
                        return self._get_fallback_reply(customer)
                        
            except httpx.TimeoutException as e:
                retry_count += 1
                last_error = e
                if retry_count <= max_retries:
                    logger.warning(f"LLM调用超时，正在进行第 {retry_count} 次重试...")
                    await asyncio.sleep(1)
                else:
                    logger.error(f"调用LLM超时，已重试 {max_retries} 次: {e}")
                    return self._get_fallback_reply(customer)
            except httpx.NetworkError as e:
                retry_count += 1
                last_error = e
                if retry_count <= max_retries:
                    logger.warning(f"LLM网络错误，正在进行第 {retry_count} 次重试: {e}")
                    await asyncio.sleep(1)
                else:
                    logger.error(f"调用LLM网络错误，已重试 {max_retries} 次: {e}")
                    return self._get_fallback_reply(customer)
            except Exception as e:
                logger.error(f"调用LLM失败: {e}")
                return self._get_fallback_reply(customer)
        
        # 如果所有重试都失败了
        logger.error(f"调用LLM失败，所有重试已用尽: {last_error}")
        return self._get_fallback_reply(customer)
    
    async def generate_reply(self, customer: Customer, messages: List[Message], 
                            knowledge_base: Optional[str] = None, db=None) -> str:
        """
        生成智能回复（自动选择智能体）
        """
        # 检查并刷新配置
        self._maybe_refresh_config()
        
        # 如果有数据库会话，尝试获取匹配的智能体
        agent = None
        if db:
            agent = self.get_agent_for_customer(customer, db)
            if agent:
                print(f"[AI回复] 使用智能体: {agent.name}")
        
        return await self.generate_reply_with_agent(customer, messages, agent, knowledge_base, db)
    
    async def generate_simple_reply(self, prompt: str) -> Optional[str]:
        """单轮简单调用，不需要客户/消息上下文（用于 AI 记忆总结等）"""
        self._maybe_refresh_config()
        
        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 800
        }
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url.rstrip('/')}/chat/completions"
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return result["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"generate_simple_reply 失败: {e}")
        return None
    
    def _build_system_prompt(self, customer: Customer, knowledge_base: Optional[str]) -> str:
        """构建系统提示词"""
        category_names = {
            "new": "新客户",
            "lead": "意向客户", 
            "returning": "老客户"
        }
        
        customer_name = _get_customer_attr(customer, 'name') or '未知'
        customer_phone = _get_customer_attr(customer, 'phone')
        customer_category = _get_customer_attr(customer, 'category', 'new')
        
        prompt = f"""你是小芃科技的智能客服助手。请根据客户信息和对话历史，提供专业、友好的回复。

客户信息：
- 姓名：{customer_name}
- 电话：{customer_phone}
- 类型：{category_names.get(customer_category, '新客户')}

回复要求：
1. 语气亲切专业，符合客服身份
2. 回复简洁明了，控制在100字以内
3. 根据客户类型调整沟通策略：
   - 新客户：热情欢迎，介绍公司优势
   - 意向客户：了解需求，推荐合适产品
   - 老客户：维护关系，提供增值服务
4. 如果无法回答，引导客户联系人工客服
"""
        
        if knowledge_base:
            prompt += f"\n\n知识库参考：\n{knowledge_base}\n"
        
        # 添加附件使用说明 - AI 主动控制附件发送
        prompt += """
【附件发送功能】
当客户询问图片、界面、效果、示例等内容时，知识库中可能包含相关附件（以[附件: 名称 | 路径]格式标记）。
如果你认为需要发送附件给客户，请在回复末尾添加以下标记：
[SEND_ATTACHMENT:附件名称]
例如：[SEND_ATTACHMENT:数字人界面]
系统会自动将该附件发送给客户。
"""
        
        return prompt
    
    def _get_fallback_reply(self, customer: Customer) -> str:
        """获取默认回复"""
        customer_category = _get_customer_attr(customer, 'category', 'new')
        fallbacks = {
            "new": f"您好！感谢您联系小芃科技。我是智能客服助手，请问有什么可以帮您？",
            "lead": f"您好！欢迎再次咨询。请问您对哪方面产品感兴趣？",
            "returning": f"您好！欢迎回来。有什么可以为您服务的吗？"
        }
        return fallbacks.get(customer_category, fallbacks["new"])
    
    async def analyze_intent(self, message_content: str) -> Dict:
        """分析用户意图"""
        # 检查并刷新配置
        self._maybe_refresh_config()
        
        max_retries = 1
        retry_count = 0
        
        while retry_count <= max_retries:
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": self.model,
                            "messages": [
                                {"role": "system", "content": "分析用户消息意图，返回JSON格式：{\"intent\": \"询价/咨询/投诉/其他\", \"urgency\": \"高/中/低\", \"keywords\": [\"关键词1\", \"关键词2\"]}"},
                                {"role": "user", "content": message_content}
                            ],
                            "temperature": 0.3
                        },
                        timeout=10.0
                    )
                    
                    if response.status_code == 200:
                        result = response.json()
                        content = result["choices"][0]["message"]["content"]
                        # 简单解析JSON
                        import json
                        try:
                            return json.loads(content)
                        except json.JSONDecodeError:
                            logger.warning(f"意图分析返回非JSON格式: {content}")
                            return {"intent": "未知", "urgency": "中", "keywords": []}
                    elif response.status_code in [429, 502, 503, 504]:  # 可重试的错误码
                        retry_count += 1
                        if retry_count <= max_retries:
                            logger.warning(f"意图分析API返回 {response.status_code}，正在进行第 {retry_count} 次重试...")
                            await asyncio.sleep(1)
                            continue
                        else:
                            logger.error(f"意图分析API错误: {response.status_code} - {response.text}")
                            return {"intent": "未知", "urgency": "中", "keywords": []}
                    else:
                        logger.error(f"意图分析API错误: {response.status_code} - {response.text}")
                        return {"intent": "未知", "urgency": "中", "keywords": []}
                    
            except httpx.TimeoutException as e:
                retry_count += 1
                if retry_count <= max_retries:
                    logger.warning(f"意图分析超时，正在进行第 {retry_count} 次重试...")
                    await asyncio.sleep(1)
                else:
                    logger.error(f"意图分析超时，已重试 {max_retries} 次: {e}")
                    return {"intent": "未知", "urgency": "中", "keywords": []}
            except httpx.NetworkError as e:
                retry_count += 1
                if retry_count <= max_retries:
                    logger.warning(f"意图分析网络错误，正在进行第 {retry_count} 次重试: {e}")
                    await asyncio.sleep(1)
                else:
                    logger.error(f"意图分析网络错误，已重试 {max_retries} 次: {e}")
                    return {"intent": "未知", "urgency": "中", "keywords": []}
            except Exception as e:
                logger.error(f"意图分析失败: {e}")
                return {"intent": "未知", "urgency": "中", "keywords": []}
        
        return {"intent": "未知", "urgency": "中", "keywords": []}
    
    def analyze_document(self, content: str) -> dict:
        """
        使用 AI 分析知识库文档内容，自动生成分类、摘要和关键词。
        返回: {"category": str, "summary": str, "keywords": List[str]}
        遇到认证错误时抛出异常，让调用方处理。
        """
        self._maybe_refresh_config()
        
        if not self.api_key:
            raise ValueError("未配置大模型 API Key，请先在系统管理中配置")
        
        # 截取前 3000 字作为分析内容（避免过长）
        analysis_content = content[:3000]
        
        prompt = f"""请分析以下文档内容，输出结构化信息：

文档内容：
{analysis_content}

请严格按照以下 JSON 格式输出（不要有任何额外文字）：
{{
    "category": "文档分类，只能是以下之一：general(通用), product(产品), service(服务), faq(常见问题)",
    "summary": "100字以内的中文摘要",
    "keywords": ["关键词1", "关键词2", "关键词3", "关键词4", "关键词5"]
}}
"""
        
        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 600
        }
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        try:
            with httpx.Client(timeout=30) as client:
                url = f"{self.base_url.rstrip('/')}/chat/completions"
                resp = client.post(url, json=payload, headers=headers)
                
                if resp.status_code == 200:
                    result = resp.json()
                    raw_content = result["choices"][0]["message"]["content"]
                    
                    # 解析 JSON
                    import json
                    import re
                    
                    # 尝试从回复中提取 JSON
                    json_match = re.search(r'\{.*\}', raw_content, re.DOTALL)
                    if json_match:
                        data = json.loads(json_match.group())
                        return {
                            "category": data.get("category", "general"),
                            "summary": data.get("summary", "")[:200],
                            "keywords": data.get("keywords", [])[:10]
                        }
                elif resp.status_code in (401, 403):
                    error_data = resp.json() if resp.text else {}
                    error_msg = error_data.get("error", {}).get("message", "API Key 认证失败")
                    raise PermissionError(f"大模型 API 认证失败 ({resp.status_code}): {error_msg}。请检查 API Key 是否有效。")
                else:
                    error_text = resp.text[:200]
                    raise RuntimeError(f"大模型 API 请求失败 ({resp.status_code}): {error_text}")
        except (PermissionError, ValueError):
            raise
        except Exception as e:
            logger.error(f"文档 AI 分析失败: {e}")
            raise RuntimeError(f"AI 分析请求失败: {e}")


# 全局实例
_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    """获取LLM服务实例"""
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
