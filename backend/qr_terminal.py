"""
终端 QR 码捕获和渲染模块
将终端中的 ASCII QR 码转换为图片
"""
import subprocess
import threading
import time
import re
from typing import Optional, Callable
import io
import base64


class TerminalQRCapture:
    """捕获终端 QR 码并转换为图片"""
    
    def __init__(self):
        self.login_process: Optional[subprocess.Popen] = None
        self.qr_lines: list = []
        self.is_capturing = False
        self.on_qr_captured: Optional[Callable[[str], None]] = None
        self.on_login_success: Optional[Callable[[], None]] = None
    
    def start_login(self, on_qr_captured: Callable[[str], None] = None,
                    on_login_success: Callable[[], None] = None) -> bool:
        """
        启动登录进程并捕获 QR 码
        
        Args:
            on_qr_captured: QR 码捕获回调，参数为 base64 图片
            on_login_success: 登录成功回调
        
        Returns:
            bool: 是否成功启动
        """
        import os
        
        self.on_qr_captured = on_qr_captured
        self.on_login_success = on_login_success
        
        # 确保 PATH 包含 whatsapp-cli
        env = os.environ.copy()
        whatsapp_bin = os.path.expanduser("~/.local/bin")
        if whatsapp_bin not in env.get("PATH", ""):
            env["PATH"] = f"{whatsapp_bin}:{env.get('PATH', '')}"
        
        # 设置终端宽度，防止QR码行被截断
        env["COLUMNS"] = "200"
        
        try:
            # 启动登录进程
            self.login_process = subprocess.Popen(
                ["whatsapp", "auth", "login"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                bufsize=1,
                universal_newlines=True
            )
            
            self.is_capturing = True
            self.qr_lines = []
            
            # 启动捕获线程
            capture_thread = threading.Thread(target=self._capture_output)
            capture_thread.daemon = True
            capture_thread.start()
            
            # 启动监控线程
            monitor_thread = threading.Thread(target=self._monitor_process)
            monitor_thread.daemon = True
            monitor_thread.start()
            
            return True
            
        except Exception as e:
            print(f"启动登录进程失败: {e}")
            return False
    
    def _capture_output(self):
        """捕获进程输出"""
        qr_start_pattern = re.compile(r'^[\s█▀▄]+\s*$')
        qr_line_pattern = re.compile(r'^[\s█▀▄]+\s*$')
        
        capturing_qr = False
        
        try:
            while self.is_capturing and self.login_process:
                line = self.login_process.stdout.readline()
                
                if not line:
                    time.sleep(0.1)
                    continue
                
                line = line.rstrip()
                print(f"[CLI] {line}")  # 调试输出
                
                # 检测 QR 码开始
                if "Scan this QR code" in line:
                    capturing_qr = True
                    self.qr_lines = []
                    print(f"[QR Capture] QR 码开始")
                    continue
                
                # 捕获 QR 码行
                if capturing_qr:
                    # QR 码行包含 Unicode 块字符
                    if '█' in line or '▀' in line or '▄' in line:
                        # 检查行长度，跳过被截断的行（正常应为65字符）
                        if len(line) < 60:
                            print(f"[QR Capture] 跳过短行 ({len(line)} 字符): {line[:30]}...")
                            continue
                        
                        self.qr_lines.append(line)
                        print(f"[QR Capture] 捕获行 {len(self.qr_lines)}: {len(line)} 字符")
                        
                        # 当捕获到一组完整的 QR 码（33行）时，立即处理
                        if len(self.qr_lines) == 33:
                            print(f"[QR Capture] QR 码完成（33行），立即处理")
                            capturing_qr = False
                            self._process_qr_code()
                            # 只保留第一个QR码，不再继续捕获
                            break
                            
                    elif line.strip() == '' and len(self.qr_lines) > 0:
                        # 空行，忽略
                        pass
                    elif len(self.qr_lines) > 100:
                        # 防止无限捕获
                        print(f"[QR Capture] 达到最大行数 {len(self.qr_lines)}，强制处理")
                        capturing_qr = False
                        self._process_qr_code()
                
                # 检测登录成功
                if "Authenticated!" in line or "connected to WhatsApp" in line:
                    self.is_capturing = False
                    if self.on_login_success:
                        self.on_login_success()
                    break
                    
        except Exception as e:
            print(f"捕获输出错误: {e}")
    
    def _process_qr_code(self):
        """处理捕获的 QR 码"""
        print(f"[QR Capture] 处理 QR 码，共 {len(self.qr_lines)} 行")
        
        if not self.qr_lines:
            print("[QR Capture] 没有 QR 码行")
            return
        
        # 保存 QR 码行以便外部访问
        self.has_qr = True
        
        # 将 ASCII QR 码转换为图片
        qr_image = self._ascii_qr_to_image(self.qr_lines)
        
        if qr_image:
            print(f"[QR Capture] QR 码图片已生成")
            self.qr_image = qr_image  # 保存图片以便外部获取
            if self.on_qr_captured:
                self.on_qr_captured(qr_image)
        else:
            print(f"[QR Capture] QR 码图片生成失败")
    
    def _ascii_qr_to_image(self, qr_lines: list) -> Optional[str]:
        """
        将 ASCII QR 码转换为 base64 图片
        
        Args:
            qr_lines: QR 码的 ASCII 行列表
        
        Returns:
            str: base64 编码的图片，或 None
        """
        try:
            from PIL import Image, ImageDraw
            
            # 计算尺寸
            height = len(qr_lines)
            width = max(len(line) for line in qr_lines) if qr_lines else 0
            
            print(f"[QR Capture] 图片尺寸: {width}x{height}")
            
            if width == 0 or height == 0:
                print("[QR Capture] 尺寸为 0")
                return None
            
            # 每个字符的像素大小
            pixel_size = 8
            
            # QR码使用半高字符(▀和▄)，每个字符代表2个像素行
            # 为了保持1:1比例，我们将每个字符映射为1x2像素
            scale = 8  # 放大倍数，让QR码更清晰
            
            # 添加白色边框（quiet zone），至少4个模块宽度
            quiet_zone = 4 * scale
            
            img_width = width * scale + 2 * quiet_zone
            img_height = height * 2 * scale + 2 * quiet_zone
            print(f"[QR Capture] 创建图片: {img_width}x{img_height} (含白色边框)")
            
            img = Image.new('RGB', (img_width, img_height), 'white')
            draw = ImageDraw.Draw(img)
            
            # 绘制 QR 码（带偏移，留出白色边框）
            black_count = 0
            for y, line in enumerate(qr_lines):
                for x, char in enumerate(line):
                    # 每个字符对应2行像素，加上边框偏移
                    top_y = y * 2 * scale + quiet_zone
                    bottom_y = (y * 2 + 1) * scale + quiet_zone
                    left_x = x * scale + quiet_zone
                    right_x = (x + 1) * scale - 1 + quiet_zone
                    
                    # Unicode 块字符映射
                    if char == '█':  # 全块 - 上下都黑
                        draw.rectangle([left_x, top_y, right_x, bottom_y + scale - 1], fill='black')
                        black_count += 2
                    elif char == '▀':  # 上半块 - 只有上半部分黑
                        draw.rectangle([left_x, top_y, right_x, top_y + scale - 1], fill='black')
                        black_count += 1
                    elif char == '▄':  # 下半块 - 只有下半部分黑
                        draw.rectangle([left_x, bottom_y, right_x, bottom_y + scale - 1], fill='black')
                        black_count += 1
                    # 空格 = 全白，不绘制
            
            print(f"[QR Capture] 绘制了 {black_count} 个黑色块")
            
            # 转换为 base64
            buffered = io.BytesIO()
            img.save(buffered, format="PNG")
            img_str = base64.b64encode(buffered.getvalue()).decode()
            
            return f"data:image/png;base64,{img_str}"
            
        except Exception as e:
            print(f"转换 QR 码失败: {e}")
            return None
    
    def _monitor_process(self):
        """监控进程状态"""
        if not self.login_process:
            return
        
        try:
            # 等待进程结束
            self.login_process.wait(timeout=120)
            
            # 进程结束，清理
            self.is_capturing = False
            
            # 检查是否成功
            if self.login_process.returncode == 0:
                if self.on_login_success:
                    self.on_login_success()
            
        except subprocess.TimeoutExpired:
            # 超时，终止进程
            self.stop()
        except Exception as e:
            print(f"监控进程错误: {e}")
    
    def stop(self):
        """停止登录进程"""
        self.is_capturing = False
        
        if self.login_process and self.login_process.poll() is None:
            try:
                self.login_process.terminate()
                self.login_process.wait(timeout=5)
            except:
                try:
                    self.login_process.kill()
                except:
                    pass
        
        self.login_process = None
        self.qr_lines = []
    
    def is_running(self) -> bool:
        """检查是否正在登录"""
        return self.is_capturing and self.login_process is not None


# 全局实例
_qr_capture: Optional[TerminalQRCapture] = None


def get_qr_capture() -> TerminalQRCapture:
    """获取 QR 码捕获器实例"""
    global _qr_capture
    if _qr_capture is None:
        _qr_capture = TerminalQRCapture()
    return _qr_capture
