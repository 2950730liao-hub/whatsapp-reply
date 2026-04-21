#!/usr/bin/env python3
"""
WhatsApp 智能客户系统 - 启动脚本
"""
import os
import sys
import subprocess
from pathlib import Path

# 确保 whatsapp-cli 在 PATH 中
WHATSAPP_BIN = Path.home() / ".local" / "bin"
if str(WHATSAPP_BIN) not in os.environ.get("PATH", ""):
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
    print("请运行: curl -fsSL https://raw.githubusercontent.com/eddmann/whatsapp-cli/main/install.sh | sh")
    return False


def check_auth():
    """检查 WhatsApp 登录状态"""
    try:
        result = subprocess.run(
            ["whatsapp", "auth", "status", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=5
        )
        import json
        data = json.loads(result.stdout)
        
        if data.get("connected"):
            print("✅ WhatsApp 已登录")
            return True
        else:
            print("⚠️  WhatsApp 未登录")
            print("请运行: whatsapp auth login")
            print("然后扫描 QR 码完成登录")
            return False
    except Exception as e:
        print(f"⚠️  无法检查登录状态: {e}")
        return False


def setup_backend():
    """设置后端环境"""
    backend_dir = Path(__file__).parent / "backend"
    
    # 创建数据目录
    data_dir = backend_dir / "data"
    data_dir.mkdir(exist_ok=True)
    
    # 创建 .env 文件
    env_file = backend_dir / ".env"
    if not env_file.exists():
        env_example = backend_dir / ".env.example"
        if env_example.exists():
            env_file.write_text(env_example.read_text())
            print("✅ 已创建 .env 文件")
    
    # 安装依赖
    print("📦 安装后端依赖...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=backend_dir,
        capture_output=True
    )
    
    if result.returncode == 0:
        print("✅ 依赖安装完成")
    else:
        print("⚠️  依赖安装可能有问题，请检查")
        print(result.stderr.decode())


def start_server():
    """启动服务器"""
    backend_dir = Path(__file__).parent / "backend"
    
    print("\n" + "="*50)
    print("🚀 启动 WhatsApp 智能客户系统")
    print("="*50 + "\n")
    
    # 检查 whatsapp-cli
    if not check_whatsapp_cli():
        return
    
    # 检查登录状态
    check_auth()
    
    # 设置环境
    setup_backend()
    
    print("\n📡 启动 API 服务器...")
    print("访问地址: http://localhost:8000")
    print("API 文档: http://localhost:8000/docs")
    print("管理界面: http://localhost:8000/static/index.html")
    print("\n按 Ctrl+C 停止服务器\n")
    
    # 启动服务器
    try:
        subprocess.run(
            [sys.executable, "-m", "uvicorn", "main:app", 
             "--host", "0.0.0.0", 
             "--port", "8000",
             "--reload"],
            cwd=backend_dir
        )
    except KeyboardInterrupt:
        print("\n\n🛑 服务器已停止")


if __name__ == "__main__":
    start_server()
