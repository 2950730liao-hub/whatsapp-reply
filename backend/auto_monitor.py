#!/usr/bin/env python3
"""
WhatsApp CRM 自动监控脚本
自动处理常见问题，减少人工干预
"""

import subprocess
import time
import os
import sys

# 配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = "/tmp/server.log"
DB_PATH = os.path.join(BASE_DIR, "data", "whatsapp_crm.db")

def run_sql(query):
    """执行 SQL 查询"""
    result = subprocess.run(
        ["sqlite3", DB_PATH, query],
        capture_output=True,
        text=True
    )
    return result.stdout.strip()

def check_server():
    """检查服务器状态"""
    try:
        result = subprocess.run(
            ["curl", "-s", "http://localhost:8000/api/customers"],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

def restart_server():
    """重启服务器"""
    print("[监控] 服务器无响应，正在重启...")
    # 查找并终止占用 8000 端口的进程
    try:
        result = subprocess.run(
            ["lsof", "-ti:8000"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            for pid in pids:
                if pid:
                    subprocess.run(["kill", "-9", pid], capture_output=True)
    except FileNotFoundError:
        pass
    time.sleep(2)
    # 启动服务器
    with open(LOG_FILE, "w") as log_file:
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
            cwd=BASE_DIR,
            stdout=log_file,
            stderr=subprocess.STDOUT
        )
    time.sleep(5)
    print("[监控] 服务器已重启")

def get_recent_logs(lines=20):
    """获取最近日志"""
    try:
        with open(LOG_FILE, "r") as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:])
    except FileNotFoundError:
        return ""

def main():
    """主监控循环"""
    print("=" * 50)
    print("WhatsApp CRM 自动监控系统")
    print("=" * 50)
    print(f"数据库: {DB_PATH}")
    print(f"日志文件: {LOG_FILE}")
    print("按 Ctrl+C 停止监控")
    print("=" * 50)
    
    check_interval = 30  # 检查间隔（秒）
    
    try:
        while True:
            # 检查服务器状态
            if not check_server():
                restart_server()
            
            # 显示状态
            logs = get_recent_logs(5)
            if "AI回复" in logs:
                print(f"[{time.strftime('%H:%M:%S')}] ✓ 系统正常运行中...")
            
            time.sleep(check_interval)
            
    except KeyboardInterrupt:
        print("\n[监控] 已停止")

if __name__ == "__main__":
    main()
