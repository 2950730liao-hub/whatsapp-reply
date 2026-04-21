"""
AI 记忆机制 - 对话总结沉淀，迭代沟通能力与成单能力

数据存储结构:
  data/ai_memory/
    communication_skills.json   # 沟通技巧沉淀（每次对话总结后写入）
    deal_patterns.json          # 成单/失单模式记录
"""
import os
import json
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

# 数据目录
_MEMORY_DIR = os.path.join(os.path.dirname(__file__), "data", "ai_memory")
_SKILLS_FILE = os.path.join(_MEMORY_DIR, "communication_skills.json")
_DEALS_FILE = os.path.join(_MEMORY_DIR, "deal_patterns.json")

# 每类记忆最多保留条数
MAX_SKILLS = 50
MAX_DEALS = 30


def _ensure_dir():
    os.makedirs(_MEMORY_DIR, exist_ok=True)


def _load_json(path: str) -> List[dict]:
    """安全读取 JSON 文件，返回列表"""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"读取记忆文件失败 [{path}]: {e}")
    return []


def _save_json(path: str, data: list):
    """安全写入 JSON 文件"""
    _ensure_dir()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"写入记忆文件失败 [{path}]: {e}")


# ============ 公开 API ============

def get_communication_tips(limit: int = 3) -> str:
    """
    获取最近的沟通经验，用于注入 AI System Prompt。
    返回格式化的经验文本，最多 limit 条。
    """
    skills = _load_json(_SKILLS_FILE)
    if not skills:
        return ""

    # 取最近且有效的条目
    recent = [s for s in reversed(skills) if s.get("tips")][:limit]
    if not recent:
        return ""

    lines = ["【历史沟通经验（请参考以下技巧优化本次回复）】"]
    for i, s in enumerate(recent, 1):
        tip = s.get("tips", "")
        if isinstance(tip, list):
            tip = "；".join(tip)
        lines.append(f"{i}. {tip}")
    return "\n".join(lines)


def get_all_memories() -> Dict[str, list]:
    """获取全部记忆内容（用于后台展示）"""
    return {
        "communication_skills": _load_json(_SKILLS_FILE),
        "deal_patterns": _load_json(_DEALS_FILE)
    }


def delete_memory_entry(memory_type: str, index: int) -> bool:
    """删除指定类型的第 index 条记忆（0-based）"""
    path = _SKILLS_FILE if memory_type == "communication_skills" else _DEALS_FILE
    data = _load_json(path)
    if 0 <= index < len(data):
        data.pop(index)
        _save_json(path, data)
        return True
    return False


def clear_all_memories():
    """清空所有记忆（重置训练）"""
    _save_json(_SKILLS_FILE, [])
    _save_json(_DEALS_FILE, [])


async def summarize_conversation(customer_id: int, messages: List[dict]) -> Optional[dict]:
    """
    使用 LLM 总结一段对话，提取沟通经验并写入记忆文件。

    messages 格式: [{"role": "user"|"assistant", "content": "..."}]
    返回总结结果 dict，或 None（失败时）。
    """
    if len(messages) < 2:
        logger.debug(f"对话太短，跳过总结 (customer_id={customer_id})")
        return None

    # 构建对话文本
    conversation_text = "\n".join([
        f"{'客户' if m['role'] == 'user' else 'AI助手'}: {m['content']}"
        for m in messages[-20:]  # 最多取最近 20 条
    ])

    prompt = f"""请分析以下销售对话，提取关键信息，以 JSON 格式返回。

对话内容：
{conversation_text}

请返回以下格式的 JSON（不要有任何额外文字）：
{{
  "tips": ["有效话术或引导技巧1", "技巧2"],
  "deal_result": "成单" 或 "未成单" 或 "跟进中",
  "key_turning_point": "对话中的关键转折点（一句话描述）",
  "customer_concerns": ["客户关注点1", "关注点2"],
  "improvement": "下次可以改进的地方（一句话）"
}}"""

    try:
        from llm_service import get_llm_service
        llm = get_llm_service()

        # 使用默认提供商生成总结
        summary_text = await llm.generate_simple_reply(prompt)
        if not summary_text:
            return None

        # 解析 JSON
        import re
        json_match = re.search(r'\{[\s\S]*\}', summary_text)
        if not json_match:
            logger.warning(f"AI 记忆总结：无法解析 JSON (customer_id={customer_id})")
            return None

        summary = json.loads(json_match.group())
        summary["customer_id"] = customer_id
        summary["summarized_at"] = datetime.now().isoformat()
        summary["message_count"] = len(messages)

        # 写入技巧记忆
        skills = _load_json(_SKILLS_FILE)
        skills.append(summary)
        if len(skills) > MAX_SKILLS:
            skills = skills[-MAX_SKILLS:]
        _save_json(_SKILLS_FILE, skills)

        # 写入成单模式
        if summary.get("deal_result") in ("成单", "未成单"):
            deals = _load_json(_DEALS_FILE)
            deals.append({
                "customer_id": customer_id,
                "result": summary["deal_result"],
                "key_turning_point": summary.get("key_turning_point", ""),
                "customer_concerns": summary.get("customer_concerns", []),
                "summarized_at": summary["summarized_at"]
            })
            if len(deals) > MAX_DEALS:
                deals = deals[-MAX_DEALS:]
            _save_json(_DEALS_FILE, deals)

        logger.info(f"[AI记忆] 对话总结完成: customer_id={customer_id}, result={summary.get('deal_result')}")
        return summary

    except Exception as e:
        logger.error(f"[AI记忆] 对话总结失败 (customer_id={customer_id}): {e}")
        return None
