"""
Neonize 测试脚本 - 验证 WhatsApp 连接
"""
import os
import sys
from neonize.client import NewClient
from neonize.events import MessageEv, ConnectedEv, event

# 数据目录
data_dir = "/Users/liaoyujun/Documents/开发系统文件/whatsapp 机器人/data"
os.makedirs(data_dir, exist_ok=True)

print("="*50)
print("Neonize WhatsApp 连接测试")
print("="*50)

# 初始化客户端
client = NewClient("test_bot")

connected = False

@client.event(ConnectedEv)
def on_connected(client: NewClient, event: ConnectedEv):
    global connected
    print(f"✅ 成功连接到 WhatsApp!")
    print(f"📱 设备信息: {event.device}")
    connected = True

@client.event(MessageEv)
def on_message(client: NewClient, event: MessageEv):
    message_text = event.message.conversation
    sender = event.info.message_source.sender
    print(f"📨 收到消息 from {sender}: {message_text}")
    
    # 自动回复测试
    if message_text and "测试" in message_text.lower():
        chat_jid = event.info.message_source.chat
        client.send_message(chat_jid, text="👋 Neonize 连接测试成功！")
        print(f"✅ 已回复测试消息")

def main():
    print("\n正在连接 WhatsApp...")
    print("如果是第一次运行，请扫描二维码登录\n")
    
    try:
        client.connect()
        print("\n✅ 客户端已启动，等待连接...")
        print("按 Ctrl+C 停止\n")
        event.wait()
    except KeyboardInterrupt:
        print("\n\n正在断开连接...")
        print("已停止")
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
