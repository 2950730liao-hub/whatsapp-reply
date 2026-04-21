#!/usr/bin/env python3
"""
WhatsApp 登录助手 - 在终端中完成 QR 码登录
"""
import os
import sys
import subprocess
import time

# 确保 whatsapp-cli 在 PATH 中
WHATSAPP_BIN = os.path.expanduser("~/.local/bin")
if WHATSAPP_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{WHATSAPP_BIN}:{os.environ.get('PATH', '')}"


def check_whatsapp_cli():
    """检查 whatsapp-cli 是否安装"""
    try:
        result = subprocess.run(
            ["whatsapp", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            print(f"✅ WhatsApp CLI: {result.stdout.strip()}")
            return True
    except:
        pass
    
    print("❌ whatsapp-cli 未找到")
    return False


def check_auth_status():
    """检查登录状态"""
    try:
        result = subprocess.run(
            ["whatsapp", "auth", "status", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=5
        )
        import json
        data = json.loads(result.stdout)
        return data.get("connected", False)
    except:
        return False


def login():
    """执行登录"""
    print("\n" + "="*60)
    print("📱 WhatsApp 登录")
    print("="*60 + "\n")
    
    # 检查 CLI
    if not check_whatsapp_cli():
        print("\n请安装 whatsapp-cli:")
        print("curl -fsSL https://raw.githubusercontent.com/eddmann/whatsapp-cli/main/install.sh | sh")
        return False
    
    # 检查是否已登录
    if check_auth_status():
        print("\n✅ 已经登录了！")
        return True
    
    print("\n🔄 开始登录流程...")
    print("请按以下步骤操作：\n")
    print("1. 下面会显示 QR 码")
    print("2. 打开手机 WhatsApp")
    print("3. 设置 → 关联设备 → 关联新设备")
    print("4. 扫描终端中的 QR 码")
    print("\n" + "-"*60)
    print("正在启动登录程序...")
    print("-"*60 + "\n")
    
    # 启动登录进程
    try:
        # 使用 os.system 让用户可以直接看到 QR 码
        result = os.system("whatsapp auth login")
        
        if result == 0:
            print("\n" + "="*60)
            print("✅ 登录成功！")
            print("="*60)
            
            # 验证登录
            time.sleep(2)
            if check_auth_status():
                print("\n🎉 WhatsApp 已成功连接！")
                print("\n现在可以：")
                print("1. 访问 http://localhost:8000 使用系统")
                print("2. 运行 python3 start_server.py 启动服务器")
                return True
            else:
                print("\n⚠️  登录可能未完成，请重试")
                return False
        else:
            print("\n❌ 登录失败")
            return False
            
    except KeyboardInterrupt:
        print("\n\n🛑 登录已取消")
        return False
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        return False


def logout():
    """退出登录"""
    print("\n" + "="*60)
    print("📱 WhatsApp 退出登录")
    print("="*60 + "\n")
    
    try:
        result = subprocess.run(
            ["whatsapp", "auth", "logout"],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            print("✅ 已退出登录")
            return True
        else:
            print(f"⚠️  {result.stderr or '退出失败'}")
            return False
            
    except Exception as e:
        print(f"❌ 错误: {e}")
        return False


def main():
    """主函数"""
    if len(sys.argv) > 1 and sys.argv[1] == "logout":
        logout()
    else:
        login()


if __name__ == "__main__":
    main()
