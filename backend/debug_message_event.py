"""
诊断脚本: 测试 Neonize MessageEv 事件是否正常触发
绕过所有 CRM 逻辑，直接打印原始事件
"""
import sys
import time
sys.path.insert(0, '/Users/liaoyujun/Documents/开发系统文件/whatsapp 机器人/backend')

from neonize.client import NewClient
from neonize.events import (
    MessageEv, ConnectedEv, DisconnectedEv,
    ConnectFailureEv, LoggedOutEv
)

print("=" * 60)
print("🔍 Neonize 消息事件诊断脚本")
print("=" * 60)
print("连接中，请稍候...")
print("连接成功后请从另一个手机发送消息到此账号")
print("=" * 60)

msg_count = 0

client = NewClient("whatsapp_crm")

@client.event(ConnectedEv)
def on_connected(c, event):
    print(f"\n✅ 已连接到 WhatsApp!")
    print(f"   现在请发送消息来测试...\n")

@client.event(MessageEv)
def on_message(c, event):
    global msg_count
    msg_count += 1
    print(f"\n{'='*60}")
    print(f"🔔 收到第 {msg_count} 条消息!")
    print(f"{'='*60}")
    try:
        print(f"  消息内容: {event.Message.conversation}")
        print(f"  发送者:   {event.Info.MessageSource.Sender}")
        print(f"  IsFromMe: {event.Info.MessageSource.IsFromMe}")
        print(f"  消息ID:   {event.Info.ID}")
        print(f"  时间戳:   {event.Info.Timestamp}")
    except Exception as e:
        print(f"  ❌ 解析出错: {e}")
        print(f"  原始事件: {event}")

@client.event(DisconnectedEv)
def on_disconnected(c, event):
    print(f"\n⚠️  连接断开: {event}")

@client.event(ConnectFailureEv)
def on_failure(c, event):
    print(f"\n❌ 连接失败: {event}")

print("启动客户端...")
client.connect()
