"""
WhatsApp Sync 进程管理器
自动监控和重启 whatsapp sync --follow 进程
"""
import subprocess
import time
import signal
import sys
import os
from datetime import datetime

class WhatsAppSyncManager:
    def __init__(self):
        self.process = None
        self.log_file = "/tmp/whatsapp_sync.log"
        self.last_message_count = 0
        self.last_check_time = time.time()
        
    def get_current_message_count(self):
        """获取当前数据库消息数"""
        try:
            result = subprocess.run(
                ["whatsapp", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=10
            )
            # 简单解析 JSON
            import json
            data = json.loads(result.stdout)
            return data.get("database", {}).get("messages", 0)
        except:
            return 0
    
    def is_sync_healthy(self):
        """检查 sync 是否健康"""
        # 检查进程是否存在
        if self.process is None or self.process.poll() is not None:
            return False
            
        # 检查消息数是否有变化（5分钟内应该有变化）
        current_count = self.get_current_message_count()
        current_time = time.time()
        
        # 如果消息数增加了，说明 sync 正常
        if current_count > self.last_message_count:
            self.last_message_count = current_count
            self.last_check_time = current_time
            return True
            
        # 如果超过5分钟消息数没有变化，可能卡住了
        if current_time - self.last_check_time > 300:  # 5分钟
            print(f"[{datetime.now()}] 警告: 5分钟内消息数没有变化，可能已卡住")
            return False
            
        return True
    
    def stop_sync(self):
        """停止 sync 进程"""
        if self.process and self.process.poll() is None:
            print(f"[{datetime.now()}] 停止现有 sync 进程 (PID: {self.process.pid})")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except:
                self.process.kill()
        
        # 确保没有残留的 whatsapp sync 进程
        subprocess.run(["pkill", "-f", "whatsapp sync --follow"], capture_output=True)
        time.sleep(2)
    
    def start_sync(self):
        """启动 sync 进程"""
        print(f"[{datetime.now()}] 启动 whatsapp sync --follow...")
        
        # 清空日志文件
        open(self.log_file, 'w').close()
        
        self.process = subprocess.Popen(
            ["whatsapp", "sync", "--follow"],
            stdout=open(self.log_file, 'a'),
            stderr=subprocess.STDOUT,
            start_new_session=True
        )
        
        print(f"[{datetime.now()}] sync 进程已启动 (PID: {self.process.pid})")
        
        # 等待初始化
        time.sleep(5)
        
        # 初始化消息计数
        self.last_message_count = self.get_current_message_count()
        self.last_check_time = time.time()
        
        return self.process
    
    def restart_sync(self):
        """重启 sync 进程"""
        print(f"[{datetime.now()}] 正在重启 sync...")
        self.stop_sync()
        time.sleep(2)
        return self.start_sync()
    
    def run(self):
        """主循环"""
        print("="*50)
        print("WhatsApp Sync 管理器已启动")
        print("按 Ctrl+C 停止")
        print("="*50)
        
        # 初始启动
        self.start_sync()
        
        check_interval = 30  # 每30秒检查一次
        
        try:
            while True:
                time.sleep(check_interval)
                
                if not self.is_sync_healthy():
                    print(f"[{datetime.now()}] sync 不健康，准备重启...")
                    self.restart_sync()
                else:
                    print(f"[{datetime.now()}] sync 运行正常 (消息数: {self.last_message_count})")
                    
        except KeyboardInterrupt:
            print(f"\n[{datetime.now()}] 正在停止...")
            self.stop_sync()
            print("已停止")

if __name__ == "__main__":
    manager = WhatsAppSyncManager()
    manager.run()
