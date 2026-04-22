#!/usr/bin/env python3
"""
自动重启脚本 - 当 Neonize 崩溃时自动重启服务
"""
import subprocess
import sys
import time
import os

def run_service():
    """运行服务并在崩溃时重启"""
    restart_count = 0
    max_restarts = 10
    
    while restart_count < max_restarts:
        print(f"\n{'='*60}")
        print(f"启动 WhatsApp 智能客服系统... (第 {restart_count + 1} 次尝试)")
        print(f"{'='*60}\n")
        
        try:
            process = subprocess.Popen(
                [sys.executable, "main.py"],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            
            # 读取输出并打印
            for line in process.stdout:
                print(line, end='')
            
            process.wait()
            
            if process.returncode != 0:
                restart_count += 1
                print(f"\n服务异常退出，代码: {process.returncode}")
                print(f"5秒后自动重启... ({restart_count}/{max_restarts})\n")
                time.sleep(5)
            else:
                print("\n服务正常退出")
                break
                
        except KeyboardInterrupt:
            print("\n收到中断信号，停止服务")
            if process:
                process.terminate()
            break
        except Exception as e:
            restart_count += 1
            print(f"\n发生异常: {e}")
            print(f"5秒后自动重启... ({restart_count}/{max_restarts})\n")
            time.sleep(5)
    
    if restart_count >= max_restarts:
        print(f"\n达到最大重启次数 ({max_restarts})，停止尝试")
        sys.exit(1)

if __name__ == "__main__":
    run_service()
