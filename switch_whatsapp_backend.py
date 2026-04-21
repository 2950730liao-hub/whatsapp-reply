#!/usr/bin/env python3
"""
WhatsApp 后端切换工具
在 CLI 方案和 Neonize 方案之间切换
"""
import os
import sys
import shutil

BACKEND_DIR = "/Users/liaoyujun/Documents/开发系统文件/whatsapp 机器人/backend"

def backup_file(filepath):
    """备份文件"""
    backup_path = filepath + ".backup"
    if os.path.exists(filepath) and not os.path.exists(backup_path):
        shutil.copy2(filepath, backup_path)
        print(f"✅ 已备份: {filepath}")

def restore_file(filepath):
    """恢复文件"""
    backup_path = filepath + ".backup"
    if os.path.exists(backup_path):
        shutil.copy2(backup_path, filepath)
        print(f"✅ 已恢复: {filepath}")
        return True
    return False

def use_cli_backend():
    """使用 CLI 后端（原始方案）"""
    print("="*50)
    print("切换到 CLI 后端")
    print("="*50)
    
    main_file = os.path.join(BACKEND_DIR, "main.py")
    backup_file(main_file)
    
    # 读取当前 main.py
    with open(main_file, 'r') as f:
        content = f.read()
    
    # 确保使用 CLI 导入
    if 'from whatsapp_adapter' in content:
        content = content.replace(
            'from whatsapp_adapter import get_adapter, NEONIZE_AVAILABLE',
            'from whatsapp_client import WhatsAppClient, MessageSyncer'
        )
        content = content.replace(
            'whatsapp_client = get_adapter().init()',
            'whatsapp_client = WhatsAppClient()'
        )
    
    with open(main_file, 'w') as f:
        f.write(content)
    
    print("✅ 已切换到 CLI 后端")
    print("\n请重启服务器生效:")
    print("  ./restart_server.sh")

def use_neonize_backend():
    """使用 Neonize 后端（新方案）"""
    print("="*50)
    print("切换到 Neonize 后端")
    print("="*50)
    
    # 检查 Neonize 是否安装
    try:
        import neonize
        print("✅ Neonize 已安装")
    except ImportError:
        print("❌ Neonize 未安装")
        print("\n请先安装 Neonize:")
        print("  pip install neonize")
        return
    
    main_file = os.path.join(BACKEND_DIR, "main.py")
    backup_file(main_file)
    
    # 修改 main.py 使用适配器
    with open(main_file, 'r') as f:
        content = f.read()
    
    # 替换导入
    if 'from whatsapp_client' in content and 'from whatsapp_adapter' not in content:
        content = content.replace(
            'from whatsapp_client import WhatsAppClient, MessageSyncer',
            'from whatsapp_adapter import get_adapter, NEONIZE_AVAILABLE'
        )
        content = content.replace(
            'whatsapp_client = WhatsAppClient()',
            'whatsapp_client = get_adapter().init()'
        )
    
    with open(main_file, 'w') as f:
        f.write(content)
    
    print("✅ 已切换到 Neonize 后端")
    print("\n请重启服务器生效:")
    print("  ./restart_server.sh")
    print("\n⚠️  首次使用需要扫描二维码登录 WhatsApp")

def show_status():
    """显示当前状态"""
    print("="*50)
    print("当前状态")
    print("="*50)
    
    # 检查 Neonize
    try:
        import neonize
        print("✅ Neonize 已安装")
    except ImportError:
        print("❌ Neonize 未安装")
    
    # 检查当前使用的后端
    main_file = os.path.join(BACKEND_DIR, "main.py")
    with open(main_file, 'r') as f:
        content = f.read()
    
    if 'from whatsapp_adapter' in content:
        print("🔄 当前后端: Neonize (适配器模式)")
    elif 'from whatsapp_client' in content:
        print("🔄 当前后端: CLI (原始方案)")
    else:
        print("❓ 无法确定当前后端")

def main():
    if len(sys.argv) < 2:
        print("用法:")
        print(f"  {sys.argv[0]} status     - 显示当前状态")
        print(f"  {sys.argv[0]} cli        - 切换到 CLI 后端")
        print(f"  {sys.argv[0]} neonize    - 切换到 Neonize 后端")
        print(f"  {sys.argv[0]} restore    - 恢复原始 main.py")
        return
    
    command = sys.argv[1].lower()
    
    if command == "status":
        show_status()
    elif command == "cli":
        use_cli_backend()
    elif command == "neonize":
        use_neonize_backend()
    elif command == "restore":
        main_file = os.path.join(BACKEND_DIR, "main.py")
        if restore_file(main_file):
            print("✅ 已恢复原始 main.py")
        else:
            print("❌ 找不到备份文件")
    else:
        print(f"未知命令: {command}")

if __name__ == "__main__":
    main()
