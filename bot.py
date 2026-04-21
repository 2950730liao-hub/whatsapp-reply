#!/usr/bin/env python3
# ============================================
# 警告：此文件已废弃 (DEPRECATED)
# 请使用 backend/main.py 作为系统主入口
# 此文件保留仅供参考，后续版本将移除
# ============================================

"""
WhatsApp Bot - 基于 whatsapp-cli 的机器人框架
"""

import subprocess
import json
import os
import sys
import time
import re
from typing import Optional, List, Dict, Callable
from dataclasses import dataclass
from datetime import datetime

# 确保 whatsapp-cli 在 PATH 中
os.environ["PATH"] = os.path.expanduser("~/.local/bin:") + os.environ.get("PATH", "")


@dataclass
class Message:
    """消息数据结构"""
    id: str
    chat_jid: str
    sender_jid: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool
    message_type: str = "text"


@dataclass
class Chat:
    """聊天数据结构"""
    jid: str
    name: str
    is_group: bool
    unread_count: int = 0


class WhatsAppClient:
    """WhatsApp CLI 客户端封装"""
    
    def __init__(self, store_dir: Optional[str] = None):
        self.store_dir = store_dir or os.path.expanduser("~/.config/whatsapp-cli")
        self._check_cli()
    
    def _check_cli(self):
        """检查 whatsapp-cli 是否可用"""
        try:
            result = subprocess.run(
                ["whatsapp", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                raise RuntimeError("whatsapp-cli 未正确安装")
        except FileNotFoundError:
            raise RuntimeError("whatsapp-cli 未找到，请先运行安装脚本")
    
    def _run(self, cmd: List[str], timeout: int = 30) -> dict:
        """执行 whatsapp 命令"""
        full_cmd = ["whatsapp"] + cmd + ["--format", "json"]
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
        return self._run(["auth", "status"])
    
    def is_authenticated(self) -> bool:
        """检查是否已认证"""
        try:
            status = self.auth_status()
            return status.get("connected", False)
        except:
            return False
    
    def get_chats(self, groups_only: bool = False, query: Optional[str] = None) -> List[Chat]:
        """获取聊天列表"""
        cmd = ["chats"]
        if groups_only:
            cmd.append("--groups")
        if query:
            cmd.extend(["--query", query])
        
        result = self._run(cmd)
        chats = result if isinstance(result, list) else []
        
        return [
            Chat(
                jid=c.get("jid", ""),
                name=c.get("name", "") or c.get("jid", ""),
                is_group=c.get("jid", "").endswith("@g.us"),
                unread_count=c.get("unread_count", 0)
            )
            for c in chats
        ]
    
    def get_messages(self, jid: str, limit: int = 50) -> List[Message]:
        """获取消息列表"""
        result = self._run(["messages", jid, "--limit", str(limit)])
        messages = result if isinstance(result, list) else []
        
        return [
            Message(
                id=m.get("id", ""),
                chat_jid=jid,
                sender_jid=m.get("sender_jid", ""),
                sender_name=m.get("sender_name", "") or m.get("push_name", "未知"),
                content=m.get("content", ""),
                timestamp=m.get("timestamp", ""),
                is_from_me=m.get("from_me", False),
                message_type=m.get("type", "text")
            )
            for m in messages
        ]
    
    def send_message(self, jid: str, message: str) -> bool:
        """发送消息"""
        try:
            self._run(["send", jid, message])
            return True
        except Exception as e:
            print(f"发送失败: {e}")
            return False
    
    def search_messages(self, keyword: str, jid: Optional[str] = None) -> List[Message]:
        """搜索消息"""
        cmd = ["search", keyword]
        if jid:
            cmd.extend(["--chat", jid])
        
        result = self._run(cmd)
        messages = result if isinstance(result, list) else []
        
        return [
            Message(
                id=m.get("id", ""),
                chat_jid=m.get("chat_jid", ""),
                sender_jid=m.get("sender_jid", ""),
                sender_name=m.get("sender_name", "") or m.get("push_name", "未知"),
                content=m.get("content", ""),
                timestamp=m.get("timestamp", ""),
                is_from_me=m.get("from_me", False),
                message_type=m.get("type", "text")
            )
            for m in messages
        ]


class WhatsAppBot:
    """WhatsApp 机器人框架"""
    
    def __init__(self):
        self.client = WhatsAppClient()
        self.handlers: List[tuple] = []  # (pattern, handler_func)
        self.command_handlers: Dict[str, Callable] = {}  # /command -> handler
        self.running = False
    
    def on_message(self, pattern: str):
        """装饰器：注册消息处理器"""
        def decorator(func: Callable):
            self.handlers.append((re.compile(pattern), func))
            return func
        return decorator
    
    def on_command(self, command: str):
        """装饰器：注册命令处理器"""
        def decorator(func: Callable):
            self.command_handlers[command.lower()] = func
            return func
        return decorator
    
    def check_auth(self) -> bool:
        """检查并提示认证"""
        if not self.client.is_authenticated():
            print("❌ 未登录 WhatsApp")
            print("请运行: whatsapp auth login")
            print("扫描 QR 码完成登录")
            return False
        print("✅ WhatsApp 已连接")
        return True
    
    def process_message(self, msg: Message):
        """处理单条消息"""
        # 跳过自己发送的消息
        if msg.is_from_me:
            return
        
        # 检查是否是命令
        if msg.content.startswith("/"):
            parts = msg.content[1:].split(maxsplit=1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            
            if cmd in self.command_handlers:
                try:
                    self.command_handlers[cmd](msg, args)
                except Exception as e:
                    print(f"命令处理错误: {e}")
            return
        
        # 正则匹配处理器
        for pattern, handler in self.handlers:
            match = pattern.search(msg.content)
            if match:
                try:
                    handler(msg, match)
                except Exception as e:
                    print(f"消息处理错误: {e}")
                break
    
    def poll_messages(self, jid: str, interval: int = 5):
        """轮询消息（简单实现）"""
        print(f"开始轮询: {jid}")
        seen_ids = set()
        
        # 获取历史消息作为基准
        messages = self.client.get_messages(jid, limit=10)
        for msg in messages:
            seen_ids.add(msg.id)
        
        print(f"已加载 {len(seen_ids)} 条历史消息，开始监听新消息...")
        
        while self.running:
            try:
                messages = self.client.get_messages(jid, limit=5)
                for msg in reversed(messages):  # 从旧到新处理
                    if msg.id not in seen_ids and not msg.is_from_me:
                        seen_ids.add(msg.id)
                        self.process_message(msg)
                
                time.sleep(interval)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"轮询错误: {e}")
                time.sleep(interval)
    
    def run(self, target_jid: Optional[str] = None):
        """运行机器人"""
        if not self.check_auth():
            return
        
        self.running = True
        
        if target_jid:
            self.poll_messages(target_jid)
        else:
            print("请指定要监听的聊天 JID")
            print("可用聊天:")
            chats = self.client.get_chats()
            for chat in chats[:10]:
                print(f"  {chat.jid} - {chat.name}")


# ============ 示例机器人实现 ============

def create_demo_bot() -> WhatsAppBot:
    """创建一个示例机器人"""
    bot = WhatsAppBot()
    
    @bot.on_command("help")
    def cmd_help(msg: Message, args: str):
        """帮助命令"""
        help_text = """🤖 可用命令:
/help - 显示帮助
/status - 查看状态
/echo <消息> - 回显消息
/time - 显示当前时间"""
        bot.client.send_message(msg.chat_jid, help_text)
    
    @bot.on_command("status")
    def cmd_status(msg: Message, args: str):
        """状态命令"""
        status = bot.client.auth_status()
        text = f"✅ 连接状态: 在线\n📊 数据库: {status.get('messages_count', 0)} 条消息"
        bot.client.send_message(msg.chat_jid, text)
    
    @bot.on_command("echo")
    def cmd_echo(msg: Message, args: str):
        """回显命令"""
        if args:
            bot.client.send_message(msg.chat_jid, f"📢 {args}")
        else:
            bot.client.send_message(msg.chat_jid, "用法: /echo <消息>")
    
    @bot.on_command("time")
    def cmd_time(msg: Message, args: str):
        """时间命令"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        bot.client.send_message(msg.chat_jid, f"🕐 当前时间: {now}")
    
    @bot.on_message(r"你好|嗨|hello|hi")
    def greet_handler(msg: Message, match):
        """问候处理器"""
        replies = ["你好! 👋", "嗨! 有什么可以帮你的?", "Hello! 🤖"]
        import random
        bot.client.send_message(msg.chat_jid, random.choice(replies))
    
    @bot.on_message(r"谢谢|thanks|thank you")
    def thanks_handler(msg: Message, match):
        """感谢处理器"""
        bot.client.send_message(msg.chat_jid, "不客气! 😊")
    
    return bot


if __name__ == "__main__":
    print("=" * 50)
    print("🤖 WhatsApp Bot")
    print("=" * 50)
    
    # 创建示例机器人
    bot = create_demo_bot()
    
    # 检查认证状态
    if not bot.check_auth():
        sys.exit(1)
    
    # 显示聊天列表
    print("\n📱 你的聊天列表:")
    chats = bot.client.get_chats()
    for i, chat in enumerate(chats[:15], 1):
        chat_type = "👥" if chat.is_group else "👤"
        print(f"{i}. {chat_type} {chat.name} ({chat.jid})")
    
    # 让用户选择要监听的聊天
    print("\n输入要监听的聊天编号或完整 JID (例如: 1234567890@s.whatsapp.net):")
    selection = input("> ").strip()
    
    target_jid = None
    if selection.isdigit() and 1 <= int(selection) <= len(chats):
        target_jid = chats[int(selection) - 1].jid
    elif "@" in selection:
        target_jid = selection
    
    if target_jid:
        print(f"\n🚀 启动机器人，监听: {target_jid}")
        print("按 Ctrl+C 停止\n")
        bot.run(target_jid)
    else:
        print("无效选择")
