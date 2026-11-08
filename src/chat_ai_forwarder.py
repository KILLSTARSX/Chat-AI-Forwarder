# -*- coding: utf-8 -*-
"""
聊天软件 AI 转发助手  (Chat AI Forwarder)
================================================================
在本机把聊天软件窗口中的新消息转发给任意 AI 桌面客户端，
并把 AI 的回复自动回发到聊天窗口。全部操作在本地完成。

设计约束（面向公开发布）：
  * 不写入任何硬编码的本地路径、用户名或机器信息；
  * 日志默认不落盘，落盘时也不记录消息正文、窗口标题、文件路径；
  * 不使用子进程 / 临时文件处理剪贴板；
  * 核心功能零第三方依赖（Windows API + tkinter + 标准库）；
  * UI 更新统一投递到主线程，后台线程不直接操作 Tk 控件。

作者：KILL-STAR SX     仓库：https://github.com/KILLSTARSX
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import math
import os
import queue
import re
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox

# ------------------------------------------------------------------
# 元信息
# ------------------------------------------------------------------
APP_NAME = "聊天软件 AI 转发助手"
APP_ID = "ChatAIForwarder"
AUTHOR = "KILL-STAR SX"
REPO = "https://github.com/KILLSTARSX"
VERSION = "2.0.0"
BUILD_DATE = "2026-11-08"

CHANGELOG = [
    ("v2.0.0", "重构发布：更名、中性灰紧凑界面、向导式操作流程；GDI 单次抓图与字节级检测；"
               "回复完成改为「内容变化→稳定」状态机；Win32 原生剪贴板；全链路隐私脱敏；单文件 EXE。"),
    ("v1.x", "早期版本：基于屏幕坐标与剪贴板的半自动转发脚本（已归档，不再维护）。"),
]

# 合规声明（界面与文档共用同一份文案，避免两处口径不一致）
LEGAL_LINES = [
    "本工具仅限在合法、合规的前提下，用于本人自有账号与自有内容的效率辅助。",
    "严禁用于群发广告、骚扰、刷屏、诈骗、爬取他人数据、规避平台风控等任何违法违规或侵害他人权益的行为。",
    "使用本工具即表示你已阅读并同意遵守适用法律法规及所用平台的用户协议；由此产生的一切后果由使用者自行承担。",
]
RISK_LINES = [
    "QQ、微信等国内聊天软件对高频、机械化的鼠标键盘操作设有风控检测。",
    "高频率或长时间连续使用，可能导致账号被警告、限制登录、功能受限，甚至永久封号。",
    "请自行降低频率、控制单次使用时长并充分评估风险；作者不对任何账号异常、封禁或损失承担责任。",
]

# ------------------------------------------------------------------
# DPI 感知（必须在创建任何窗口之前）
# ------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ------------------------------------------------------------------
# 运行环境：所有目录均在运行时推导，源码内不含任何绝对路径
# ------------------------------------------------------------------
def _user_data_dir() -> str:
    base = os.environ.get("APPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    path = os.path.join(base, APP_ID)
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        path = os.path.join(os.path.expanduser("~"), "." + APP_ID.lower())
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
    return path


USER_DIR = _user_data_dir()
CONFIG_PATH = os.path.join(USER_DIR, "config.json")
LOG_PATH = os.path.join(USER_DIR, "app.log")

# 可选组件目录：与 EXE 同级的 ocr_runtime/，不存在则 OCR 不可用。
# 单文件 EXE 不含重型推理引擎，需要 OCR 的用户可单独放置该目录。
def _runtime_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def app_icon_path() -> str:
    """定位窗口图标：冻结版从解包目录/EXE 同级读取，源码版从 assets 读取。"""
    candidates = []
    if getattr(sys, "frozen", False):
        unpacked = getattr(sys, "_MEIPASS", "")
        if unpacked:
            candidates.append(os.path.join(unpacked, "assets", "app.ico"))
        candidates.append(os.path.join(_runtime_root(), "assets", "app.ico"))
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(here, "assets", "app.ico"))
        candidates.append(os.path.join(os.path.dirname(here), "assets", "app.ico"))
    for path in candidates:
        try:
            if path and os.path.isfile(path):
                return path
        except Exception:
            pass
    return ""


APP_ICON = app_icon_path()


# ------------------------------------------------------------------
# 隐私工具
# ------------------------------------------------------------------
_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]*")
_USER_RE = re.compile(r"(?i)(?:users|home)[\\/][^\\/\s]+")
_DRIVE_RE = re.compile(r"(?i)\\\\[A-Za-z0-9_.-]+\\[^\s]+")


def sanitize(text) -> str:
    """日志兜底：抹去任何可能出现的本地路径、用户名、网络路径。"""
    s = str(text)
    s = _PATH_RE.sub("[路径已隐藏]", s)
    s = _USER_RE.sub("[用户已隐藏]", s)
    s = _DRIVE_RE.sub("[路径已隐藏]", s)
    return s


def fp(text) -> str:
    """内容指纹：用于去重与日志关联，不可逆、不含原文。"""
    try:
        return hashlib.blake2b(text.encode("utf-8", "ignore"), digest_size=3).hexdigest()
    except Exception:
        return "------"


class SafeLogger:
    """默认不落盘；落盘时仍强制脱敏并限制体积。"""

    LIMIT = 512 * 1024

    def __init__(self):
        self.enabled = False
        self._lock = threading.Lock()

    def write(self, line: str):
        if not self.enabled:
            return
        clean = sanitize(line)
        with self._lock:
            try:
                if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > self.LIMIT:
                    try:
                        os.replace(LOG_PATH, LOG_PATH + ".1")
                    except Exception:
                        pass
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), clean))
            except Exception:
                pass


LOGGER = SafeLogger()

# ------------------------------------------------------------------
# Windows API
# ------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_LPV = ctypes.c_void_p
_UINT = ctypes.c_uint

user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = _LPV
user32.ReleaseDC.argtypes = [wt.HWND, _LPV]
user32.ReleaseDC.restype = ctypes.c_int
gdi32.CreateCompatibleDC.argtypes = [_LPV]
gdi32.CreateCompatibleDC.restype = _LPV
gdi32.CreateCompatibleBitmap.argtypes = [_LPV, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = _LPV
gdi32.SelectObject.argtypes = [_LPV, _LPV]
gdi32.SelectObject.restype = _LPV
gdi32.DeleteObject.argtypes = [_LPV]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [_LPV]
gdi32.DeleteDC.restype = wt.BOOL
gdi32.BitBlt.argtypes = [_LPV, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         _LPV, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.BitBlt.restype = wt.BOOL
gdi32.GetDIBits.argtypes = [_LPV, _LPV, _UINT, _UINT, _LPV, _LPV, _UINT]
gdi32.GetDIBits.restype = ctypes.c_int

user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, _UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.SetFocus.argtypes = [wt.HWND]
user32.SetFocus.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, _LPV]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL
kernel32.GetCurrentThreadId.restype = wt.DWORD
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wt.BOOL
user32.mouse_event.argtypes = [wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wt.DWORD, ctypes.c_size_t]
user32.SendMessageW.argtypes = [wt.HWND, _UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.PostMessageW.argtypes = [wt.HWND, _UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.PostMessageW.restype = wt.BOOL
user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.ScreenToClient.restype = wt.BOOL

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
user32.OpenClipboard.argtypes = [wt.HWND]
user32.OpenClipboard.restype = wt.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wt.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wt.BOOL
user32.GetClipboardData.argtypes = [_UINT]
user32.GetClipboardData.restype = _LPV
user32.SetClipboardData.argtypes = [_UINT, _LPV]
user32.SetClipboardData.restype = _LPV
kernel32.GlobalAlloc.argtypes = [_UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = _LPV
kernel32.GlobalLock.argtypes = [_LPV]
kernel32.GlobalLock.restype = _LPV
kernel32.GlobalUnlock.argtypes = [_LPV]
kernel32.GlobalUnlock.restype = wt.BOOL
kernel32.GlobalFree.argtypes = [_LPV]
kernel32.GlobalFree.restype = _LPV

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
MK_LBUTTON, MK_RBUTTON = 0x0001, 0x0002
VK_RETURN, VK_CONTROL, VK_V = 0x0D, 0x11, 0x56
KEYEVENTF_KEYUP = 0x0002

SCREEN_W = user32.GetSystemMetrics(0)
SCREEN_H = user32.GetSystemMetrics(1)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


# ------------------------------------------------------------------
# 可选依赖（全部为增强项，缺失不影响核心功能）
# ------------------------------------------------------------------
def _try_import(name):
    try:
        return __import__(name)
    except Exception:
        return None


NUMPY = _try_import("numpy")
HAS_NUMPY = NUMPY is not None
UIA = _try_import("uiautomation")
HAS_UIA = UIA is not None


class OCREngine:
    """OCR 为可选侧载组件，不打包进单文件 EXE。"""

    def __init__(self):
        self.engine = None
        self.state = "未启用"
        self.last_ms = 0.0

    @property
    def available(self):
        return self.engine is not None

    def ensure(self):
        if self.engine is not None:
            return True
        root = os.path.join(_runtime_root(), "ocr_runtime")
        if os.path.isdir(root) and root not in sys.path:
            sys.path.insert(0, root)
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
            from PIL import Image  # noqa: F401
            self.engine = RapidOCR()
            self.state = "已启用"
            return True
        except Exception:
            self.state = "未安装（可选）"
            return False

    def read(self, rect, threshold=0.5):
        if not self.ensure():
            return ""
        try:
            from PIL import Image
            f = grab(*rect)
            if f is None:
                return ""
            img = Image.frombytes("RGB", (f.w, f.h), f.to_rgb())
            t0 = time.monotonic()
            result, _ = self.engine(img)
            self.last_ms = (time.monotonic() - t0) * 1000
            if not result:
                return ""
            parts, scores = [], []
            for item in result:
                try:
                    _box, txt, score = item
                    scores.append(float(score))
                    if str(txt).strip():
                        parts.append(str(txt).strip())
                except Exception:
                    continue
            if scores and sum(scores) / len(scores) < threshold:
                return ""
            return "\n".join(parts)
        except Exception:
            return ""


OCR = OCREngine()


# ------------------------------------------------------------------
# 屏幕抓取：单次 GDI BitBlt 直接得到 BGRA 字节，无 PIL / 无子进程
# ------------------------------------------------------------------
class Frame:
    __slots__ = ("w", "h", "raw")

    def __init__(self, w, h, raw):
        self.w, self.h, self.raw = w, h, raw

    def to_rgb(self):
        """BGRA -> RGB（仅 OCR 等可选功能使用）。"""
        if HAS_NUMPY:
            a = NUMPY.frombuffer(self.raw, dtype=NUMPY.uint8).reshape(self.h, self.w, 4)
            return a[:, :, [2, 1, 0]].tobytes()
        out = bytearray(self.w * self.h * 3)
        src = self.raw
        j = 0
        for i in range(0, len(src), 4):
            out[j] = src[i + 2]
            out[j + 1] = src[i + 1]
            out[j + 2] = src[i]
            j += 3
        return bytes(out)


def grab(x1, y1, x2, y2):
    x1 = max(0, min(SCREEN_W, int(x1)))
    y1 = max(0, min(SCREEN_H, int(y1)))
    x2 = max(0, min(SCREEN_W, int(x2)))
    y2 = max(0, min(SCREEN_H, int(y2)))
    w, h = x2 - x1, y2 - y1
    if w < 1 or h < 1:
        return None
    screen = user32.GetDC(0)
    if not screen:
        return None
    mem = bmp = old = None
    try:
        mem = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
        if not mem or not bmp:
            return None
        old = gdi32.SelectObject(mem, bmp)
        if not gdi32.BitBlt(mem, 0, 0, w, h, screen, x1, y1, SRCCOPY):
            return None
        buf = ctypes.create_string_buffer(w * h * 4)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        got = gdi32.GetDIBits(mem, bmp, 0, h,
                              ctypes.cast(buf, _LPV),
                              ctypes.cast(ctypes.byref(bmi), _LPV),
                              DIB_RGB_COLORS)
        if got != h:
            return None
        return Frame(w, h, buf.raw)
    except Exception:
        return None
    finally:
        try:
            if mem and old:
                gdi32.SelectObject(mem, old)
            if bmp:
                gdi32.DeleteObject(bmp)
            if mem:
                gdi32.DeleteDC(mem)
        except Exception:
            pass
        user32.ReleaseDC(0, screen)


# ------------------------------------------------------------------
# 检测算法：一次抓图同时产出整体哈希 + 网格签名
# ------------------------------------------------------------------
def grid_signature(frame: Frame, points: int) -> bytes:
    side = max(2, int(round(math.sqrt(points))))
    raw, w, h, stride = frame.raw, frame.w, frame.h, frame.w * 4
    out = bytearray(side * side * 4)
    k = 0
    for r in range(side):
        py = (r * (h - 1)) // max(1, side - 1)
        row = py * stride
        for c in range(side):
            px = (c * (w - 1)) // max(1, side - 1)
            o = row + px * 4
            out[k:k + 4] = raw[o:o + 4]
            k += 4
    return bytes(out)


def snapshot(rect, points=144):
    f = grab(*rect)
    if f is None:
        return None
    return (hashlib.blake2b(f.raw, digest_size=16).digest(),
            grid_signature(f, points))


def region_raw(rect):
    """小区域全像素 BGRA，用于复制按钮一类精细比对。"""
    f = grab(*rect)
    return f.raw if f is not None else None


def changed_ratio(a: bytes, b: bytes, tol: int = 18) -> float:
    """返回发生变化的像素比例（4 字节/像素，忽略 alpha）。"""
    if a is None or b is None or len(a) != len(b) or len(a) < 4:
        return 1.0
    n = len(a) - len(a) % 4
    if n == 0:
        return 1.0
    if HAS_NUMPY and n >= 8192:
        try:
            x = NUMPY.frombuffer(a[:n], dtype=NUMPY.uint8).reshape(-1, 4)[:, :3].astype(NUMPY.int16)
            y = NUMPY.frombuffer(b[:n], dtype=NUMPY.uint8).reshape(-1, 4)[:, :3].astype(NUMPY.int16)
            return float(NUMPY.count_nonzero(NUMPY.max(NUMPY.abs(x - y), axis=1) > tol)) / x.shape[0]
        except Exception:
            pass
    diff = 0
    for i in range(0, n, 4):
        if (abs(a[i] - b[i]) > tol or abs(a[i + 1] - b[i + 1]) > tol
                or abs(a[i + 2] - b[i + 2]) > tol):
            diff += 1
    return diff / (n // 4)


def signal_change(old, new, min_ratio, tol):
    """双通道判定：整体哈希必须不同，且网格变化比例超过阈值。"""
    if old is None or new is None:
        return True, 1.0
    if old[0] == new[0]:
        return False, 0.0
    ratio = changed_ratio(old[1], new[1], tol)
    return ratio >= min_ratio, ratio


# ------------------------------------------------------------------
# 窗口 / 输入 / 剪贴板
# ------------------------------------------------------------------
def top_window(x, y):
    h = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
    if not h:
        return 0
    root = user32.GetAncestor(h, 2)
    return root or h


def window_title(hwnd):
    if not hwnd:
        return ""
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def activate(hwnd):
    if not hwnd:
        return
    try:
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return
        ftid = user32.GetWindowThreadProcessId(fg, None)
        mtid = kernel32.GetCurrentThreadId()
        attached = False
        if ftid != mtid:
            attached = bool(user32.AttachThreadInput(mtid, ftid, True))
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
        if attached:
            user32.AttachThreadInput(mtid, ftid, False)
    except Exception:
        pass


_click_method = "cursor"
_click_jitter = False


def do_click(x, y, btn="left"):
    import random
    if _click_jitter:
        x += random.randint(-2, 2)
        y += random.randint(-2, 2)
    x, y = int(x), int(y)
    hwnd = top_window(x, y)
    if not hwnd:
        return 0
    if _click_method == "cursor":
        user32.SetCursorPos(x, y)
        time.sleep(0.04)
        down, up = ((0x0008, 0x0010) if btn == "right" else (0x0002, 0x0004))
        user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(up, 0, 0, 0, 0)
    else:
        pt = wt.POINT(x, y)
        user32.ScreenToClient(hwnd, ctypes.byref(pt))
        lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
        dn, up, wp = ((WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON) if btn == "right"
                      else (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON))
        call = user32.SendMessageW if _click_method == "sendmsg" else user32.PostMessageW
        call(hwnd, dn, wp, lp)
        time.sleep(0.03)
        call(hwnd, up, 0, lp)
    return hwnd


def key_tap(vk):
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def ctrl_v():
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    key_tap(VK_V)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def press_enter():
    key_tap(VK_RETURN)


def clip_get() -> str:
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.015)
    else:
        return ""
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p) or ""
        finally:
            kernel32.GlobalUnlock(h)
    except Exception:
        return ""
    finally:
        user32.CloseClipboard()


def clip_set(text: str) -> bool:
    data = text.encode("utf-16-le", "ignore") + b"\x00\x00"
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    p = kernel32.GlobalLock(h)
    if not p:
        kernel32.GlobalFree(h)
        return False
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.015)
    else:
        kernel32.GlobalFree(h)
        return False
    owned = True
    try:
        user32.EmptyClipboard()
        if user32.SetClipboardData(CF_UNICODETEXT, h):
            owned = False  # 所有权移交给系统
            return True
        return False
    finally:
        user32.CloseClipboard()
        if owned:
            kernel32.GlobalFree(h)


class ClipboardGuard:
    """可选：任务结束后恢复用户原有剪贴板内容。"""

    def __init__(self):
        self.enabled = True
        self.saved = None

    def snapshot(self):
        if not self.enabled or self.saved is not None:
            return
        self.saved = clip_get()

    def restore(self):
        if self.enabled and self.saved:
            clip_set(self.saved)
        self.saved = None


# ------------------------------------------------------------------
# 配置
# ------------------------------------------------------------------
def default_config():
    sw, sh = SCREEN_W, SCREEN_H
    cl = int(sw * 0.02)
    cr = int(sw * 0.52)
    my = int(sh * 0.30)
    iy = int(sh * 0.85)
    return {
        "schema": 2,
        "markers": {
            "chat_msg": {"x": cl + 60, "y": my},
            "copy_item": {"x": cl + 160, "y": my + 70},
            "ai_input": {"x": cr + 100, "y": iy},
            "ai_send": {"x": int(sw * 0.98) - 50, "y": iy},
            "ai_copy_btn": {"x": int(sw * 0.98) - 80, "y": my + 100},
            "chat_input": {"x": cl + 100, "y": iy},
            "chat_send": {"x": int(sw * 0.48) - 50, "y": iy},
        },
        "monitor_rect": [cl + 30, my - 60, int(sw * 0.22), int(sh * 0.28)],
        "reply_rect": [cr + 40, my - 60, int(sw * 0.30), int(sh * 0.34)],
        "chat_titles": "", "ai_titles": "", "guard": False,
        "read_mode": "clipboard", "run_mode": "both",
        "poll_ms": 600, "sample_points": 144, "change_tol": 18, "change_ratio": 0.04,
        "min_wait": 2.0, "reply_stable": 1.2, "reply_timeout": 180,
        "reply_poll_ms": 400, "startup_quiet": 2.5, "cooldown": 1.5,
        "click_method": "cursor", "jitter": False, "clipboard_restore": True,
        "preview_text": False, "file_log": False, "rescale": False,
        "screen_w": sw, "screen_h": sh,
    }


def load_config():
    """只读取本版本的配置，不导入任何历史文件或历史用户数据。"""
    cfg = default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = None
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in cfg:
                cfg[k] = v
        cfg.setdefault("markers", {})
        for k, v in default_config()["markers"].items():
            cfg["markers"].setdefault(k, v)
    # 目标窗口白名单永远以「空」为默认：只接受用户显式添加的字符串，
    # 任何异常值（None / list / 旧字段）一律归一为空，程序绝不自动填入窗口。
    for k in ("chat_titles", "ai_titles"):
        v = cfg.get(k)
        cfg[k] = v.strip() if isinstance(v, str) else ""
    # 白名单为空时窗口校验没有意义，强制回到关闭状态；类型异常也一律按关闭处理
    cfg["guard"] = (bool(cfg.get("guard")) and
                    bool(cfg["chat_titles"] or cfg["ai_titles"]))
    cfg["schema"] = 2
    # 可选：分辨率变化时按比例缩放（默认关闭，严格保留用户摆放的位置）
    if cfg.get("rescale"):
        ow, oh = cfg.get("screen_w") or SCREEN_W, cfg.get("screen_h") or SCREEN_H
        if ow and oh and (ow != SCREEN_W or oh != SCREEN_H):
            sx, sy = SCREEN_W / float(ow), SCREEN_H / float(oh)
            if abs(sx - 1) > 0.03 or abs(sy - 1) > 0.03:
                for p in cfg["markers"].values():
                    p["x"], p["y"] = int(p["x"] * sx), int(p["y"] * sy)
                for key in ("monitor_rect", "reply_rect"):
                    r = cfg[key]
                    cfg[key] = [int(r[0] * sx), int(r[1] * sy), int(r[2] * sx), int(r[3] * sy)]
    cfg["screen_w"], cfg["screen_h"] = SCREEN_W, SCREEN_H
    return cfg


def save_config(cfg):
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------
# 屏幕覆盖层：准星 / 区域框
# ------------------------------------------------------------------
MARKER_DEFS = [
    ("chat_msg", "1 聊天消息（右键）", "#4f8cff", "chat"),
    ("copy_item", "2 右键菜单·复制", "#38bdf8", "chat"),
    ("ai_input", "3 AI 输入框", "#ffb454", "ai"),
    ("ai_send", "4 AI 发送", "#ff7a59", "ai"),
    ("ai_copy_btn", "5 AI 复制回复", "#c084fc", "ai"),
    ("chat_input", "6 聊天输入框", "#3ddc97", "chat"),
    ("chat_send", "7 聊天发送", "#2dd4bf", "chat"),
]


class Crosshair:
    """圆形定位准星：圆内与四角完全透明，仅圆环、准线和中心徽章可见。"""
    SIZE = 38
    TRANS = "#010203"
    ALPHA_NORMAL = 0.72
    ALPHA_GHOST = 0.34

    def __init__(self, root, key, label, color, x, y, on_release):
        self.root, self.key = root, key
        self.label, self.color = label, color
        self.on_release = on_release
        self._job = None
        self._tip = None
        self._flash_job = None
        self.visible = True
        self.ghosted = False
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", self.TRANS)
        self.win.attributes("-alpha", self.ALPHA_NORMAL)
        self.win.configure(bg=self.TRANS)
        s = self.SIZE
        self.canvas = tk.Canvas(self.win, width=s, height=s, bg=self.TRANS,
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        c = s // 2
        # 外圆内部使用透明键色：100% 透出下层内容，不会遮挡聊天文字。
        # 可见部分只有圆环、四段准线和中心徽章，拖动抓这几处即可。
        self.ring = self.canvas.create_oval(2, 2, s - 3, s - 3,
                                            fill=self.TRANS,
                                            outline=self.color, width=2)
        self.canvas.create_line(c, 3, c, c - 10, fill=self.color, width=1)
        self.canvas.create_line(c, c + 10, c, s - 4, fill=self.color, width=1)
        self.canvas.create_line(3, c, c - 10, c, fill=self.color, width=1)
        self.canvas.create_line(c + 10, c, s - 4, c, fill=self.color, width=1)
        # 中心彩色徽章（不透明，作为主要拖拽命中区）
        self.canvas.create_oval(c - 9, c - 9, c + 9, c + 9,
                                fill=self.color, outline="")
        self.canvas.create_text(c, c, text=label[0], fill=BG,
                                font=("Consolas", 9, "bold"))
        for wg in (self.win, self.canvas):
            wg.bind("<Button-1>", self._start)
            wg.bind("<B1-Motion>", self._move)
            wg.bind("<ButtonRelease-1>", self._end)
            wg.bind("<Enter>", self._show_tip)
            wg.bind("<Leave>", self._hide_tip)
        self.win.geometry("+%d+%d" % (int(x), int(y)))

    def _start(self, e):
        self._ox, self._oy = e.x, e.y

    def _move(self, e):
        self.win.geometry("+%d+%d" % (self.win.winfo_x() + e.x - self._ox,
                                      self.win.winfo_y() + e.y - self._oy))

    def _end(self, _e=None):
        if self.on_release:
            if self._job:
                self.root.after_cancel(self._job)
            self._job = self.root.after(300, self.on_release)

    def _show_tip(self, _e=None):
        if self._tip:
            return
        self._tip = tk.Toplevel(self.win)
        self._tip.overrideredirect(True)
        self._tip.attributes("-topmost", True)
        tk.Label(self._tip, text=self.label, bg=CARD, fg=FG,
                 font=("Microsoft YaHei", 9), padx=8, pady=3).pack()
        self._tip.geometry("+%d+%d" % (self.win.winfo_x() + self.SIZE + 6,
                                       self.win.winfo_y()))

    def _hide_tip(self, _e=None):
        if self._tip:
            self._tip.destroy()
            self._tip = None

    def center(self):
        return (self.win.winfo_x() + self.SIZE // 2,
                self.win.winfo_y() + self.SIZE // 2)

    def pos(self):
        return self.win.winfo_x(), self.win.winfo_y()

    def set_pos(self, x, y):
        self.win.geometry("+%d+%d" % (int(x), int(y)))

    def set_visible(self, val):
        self.visible = val
        if val:
            self.win.deiconify()
        else:
            self._hide_tip()
            self.win.withdraw()

    def set_ghost(self, val):
        self.ghosted = val
        if not self._flash_job:
            self.win.attributes("-alpha", self.ALPHA_GHOST if val else self.ALPHA_NORMAL)

    def flash(self):
        """双击唤回时短暂实心高亮 + 自动弹出名称，避免用户找不到。"""
        if self._flash_job:
            try:
                self.root.after_cancel(self._flash_job)
            except Exception:
                pass
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-alpha", 0.98)
        try:
            self.canvas.itemconfig(self.ring, width=4)  # 不依赖 alpha 也能明显高亮
        except Exception:
            pass
        self._show_tip()

        def restore():
            self._flash_job = None
            try:
                self.win.attributes(
                    "-alpha", self.ALPHA_GHOST if self.ghosted else self.ALPHA_NORMAL)
                self.canvas.itemconfig(self.ring, width=2)
                self._hide_tip()
            except Exception:
                pass

        self._flash_job = self.root.after(1600, restore)

    def destroy(self):
        self._hide_tip()
        try:
            self.win.destroy()
        except Exception:
            pass


class RegionFrame:
    """中心透明、可拖动 / 缩放的区域框。"""

    TRANS = "#010203"

    def __init__(self, root, rect, label, color, on_release):
        self.root, self.label, self.color = root, label, color
        self.on_release = on_release
        self._drag = {}
        self.visible = True
        x, y, w, h = [int(v) for v in rect]
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", self.TRANS)
        self.win.configure(bg=self.TRANS)
        self.canvas = tk.Canvas(self.win, bg=self.TRANS, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.tag = tk.Label(self.win, text=" " + label + " ", bg=color, fg=BG,
                            font=("Microsoft YaHei", 8, "bold"))
        self.tag.place(x=0, y=0)
        self.handle = tk.Label(self.win, text="↘", bg=color, fg=BG,
                               font=("Consolas", 11, "bold"), cursor="sizing")
        self.handle.place(relx=1.0, rely=1.0, anchor=tk.SE, x=-1, y=-1)
        self.win.geometry("%dx%d+%d+%d" % (max(80, w), max(60, h), x, y))
        self.win.update_idletasks()
        for t in (self.canvas, self.tag):
            t.bind("<Button-1>", self._start_move)
            t.bind("<B1-Motion>", self._on_move)
        self.handle.bind("<Button-1>", self._start_resize)
        self.handle.bind("<B1-Motion>", self._on_resize)
        for t in (self.canvas, self.handle, self.tag):
            t.bind("<ButtonRelease-1>", lambda _e: self._end())
        self._draw()

    def _draw(self):
        w, h = self.win.winfo_width(), self.win.winfo_height()
        self.canvas.delete("border")
        self.canvas.create_rectangle(1, 1, w - 2, h - 2, outline=self.color,
                                     width=2, tags="border")
        self.canvas.create_rectangle(5, 5, w - 6, h - 6, outline="#ffffff",
                                     width=1, tags="border", dash=(4, 4))

    def _start_move(self, e):
        self._drag = {"a": "m", "x": e.x_root, "y": e.y_root,
                      "ox": self.win.winfo_x(), "oy": self.win.winfo_y()}

    def _on_move(self, e):
        if self._drag.get("a") == "m":
            self.win.geometry("+%d+%d" % (self._drag["ox"] + e.x_root - self._drag["x"],
                                          self._drag["oy"] + e.y_root - self._drag["y"]))

    def _start_resize(self, e):
        self._drag = {"a": "r", "x": e.x_root, "y": e.y_root,
                      "w": self.win.winfo_width(), "h": self.win.winfo_height()}

    def _on_resize(self, e):
        if self._drag.get("a") == "r":
            w = max(80, self._drag["w"] + e.x_root - self._drag["x"])
            h = max(60, self._drag["h"] + e.y_root - self._drag["y"])
            self.win.geometry("%dx%d" % (w, h))
            self._draw()

    def _end(self):
        if self.on_release:
            self.root.after(300, self.on_release)

    def rect(self):
        x, y = self.win.winfo_x(), self.win.winfo_y()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        return [x, y, x + w, y + h]

    def geometry(self):
        return [self.win.winfo_x(), self.win.winfo_y(),
                self.win.winfo_width(), self.win.winfo_height()]

    def set_geometry(self, r):
        x, y, w, h = [int(v) for v in r]
        self.win.geometry("%dx%d+%d+%d" % (max(80, w), max(60, h), x, y))
        self._draw()

    def set_visible(self, val):
        self.visible = val
        if val:
            self.win.deiconify()
        else:
            self.win.withdraw()

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


# ------------------------------------------------------------------
# 主题
# ------------------------------------------------------------------
# 中性灰阶：背景一律 R=G=B，不使用任何带蓝调的深色
BG = "#0d0d0d"
PANEL = "#171717"
CARD = "#222222"
LINE = "#363636"
FG = "#e8e8e8"
MUTED = "#8c8c8c"
ACCENT = "#4f8cff"       # 仅作强调/链接色，不作为背景
OK = "#3ddc97"
WARN = "#ffb454"
ERR = "#ff6b6b"
ENTRY_BG = "#0a0a0a"
BTN_HOVER = "#333333"
BTN_OFF_BG = "#242424"
BTN_OFF_FG = "#5e5e5e"
LOG_BG = "#080808"
LOG_FG = "#c4c4c4"
FONT = ("Microsoft YaHei", 9)
MONO = ("Consolas", 9)


def setup_theme(root):
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except Exception:
        pass
    st.configure(".", background=BG, foreground=FG, font=FONT)
    st.configure("TFrame", background=BG)
    st.configure("Card.TFrame", background=PANEL, relief=tk.FLAT)
    st.configure("TLabel", background=BG, foreground=FG, font=FONT)
    st.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Microsoft YaHei", 8))
    st.configure("Card.Muted.TLabel", background=PANEL, foreground=MUTED, font=("Microsoft YaHei", 8))
    st.configure("Card.TLabel", background=PANEL, foreground=FG, font=FONT)
    st.configure("Title.TLabel", background=PANEL, foreground=FG, font=("Microsoft YaHei", 13, "bold"))
    st.configure("TLabelframe", background=PANEL, bordercolor=LINE, relief=tk.SOLID, borderwidth=1)
    st.configure("TLabelframe.Label", background=PANEL, foreground=ACCENT,
                 font=("Microsoft YaHei", 9, "bold"))
    st.configure("TButton", background=CARD, foreground=FG, bordercolor=LINE,
                 borderwidth=1, padding=(9, 5), font=FONT)
    st.map("TButton", background=[("active", BTN_HOVER), ("disabled", BTN_OFF_BG)],
           foreground=[("disabled", BTN_OFF_FG)])
    st.configure("TNotebook", background=BG, borderwidth=0)
    st.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                 padding=(16, 7), borderwidth=0, font=FONT)
    st.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", FG)])
    st.configure("TEntry", fieldbackground=ENTRY_BG, foreground=FG,
                 insertcolor=FG, bordercolor=LINE, padding=4)
    st.configure("TCheckbutton", background=PANEL, foreground=FG, font=FONT)
    st.map("TCheckbutton", background=[("active", PANEL)],
           indicatorcolor=[("selected", ACCENT), ("!selected", ENTRY_BG)])
    st.configure("TRadiobutton", background=PANEL, foreground=FG, font=FONT)
    st.map("TRadiobutton", background=[("active", PANEL)],
           indicatorcolor=[("selected", ACCENT), ("!selected", ENTRY_BG)])
    st.configure("Horizontal.TScale", background=PANEL, troughcolor=ENTRY_BG,
                 bordercolor=LINE, lightcolor=ACCENT, darkcolor=ACCENT)
    st.configure("TScrollbar", background=CARD, troughcolor=BG, bordercolor=LINE)
    st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG)
    root.configure(bg=BG)
    return st


def scrollable(parent):
    outer = ttk.Frame(parent, style="TFrame")
    outer.pack(fill=tk.BOTH, expand=True)
    cv = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
    sb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=cv.yview)
    cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    sb.pack(side=tk.RIGHT, fill=tk.Y)
    cv.configure(yscrollcommand=sb.set)
    inner = ttk.Frame(cv, style="TFrame")
    win_id = cv.create_window((0, 0), window=inner, anchor=tk.NW)

    def _resize(_e=None):
        cv.configure(scrollregion=cv.bbox("all"))
        try:
            cv.itemconfig(win_id, width=cv.winfo_width())
        except Exception:
            pass

    inner.bind("<Configure>", _resize)
    cv.bind("<Configure>", _resize)
    return inner


def wheel_scroll(event, canvas):
    try:
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    except Exception:
        pass


# ------------------------------------------------------------------
# 主程序
# ------------------------------------------------------------------
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("%s  %s" % (APP_NAME, VERSION))
        if APP_ICON:
            try:
                self.root.iconbitmap(APP_ICON)             # 本窗口标题栏图标
                self.root.iconbitmap(default=APP_ICON)     # 后续弹窗继承同一图标
            except Exception:
                pass
        self.root.geometry("940x700")
        self.root.minsize(800, 580)
        self._main_thread = threading.current_thread()
        setup_theme(self.root)

        self.cfg = load_config()
        LOGGER.enabled = bool(self.cfg.get("file_log"))
        self.clipguard = ClipboardGuard()
        self.clipguard.enabled = bool(self.cfg.get("clipboard_restore"))
        self._uiq = queue.Queue()
        self.vars = {}
        self.slider_labels = {}
        self._slider_frames = {}
        self._slider_fmts = {}
        self._save_job = None
        self.running = False
        self.paused = False
        self.worker = None
        self.msg_count = 0
        self.last_ok = "-"
        self.last_err = "-"
        self.last_mode = "-"
        self.last_ms = 0.0
        self.state_text = "待机"
        self.seen = OrderedDict()
        self.last_reply_hash = ""
        self._guard_cache = {"chat": 0, "ai": 0}
        self.last_overlay_state = True
        self.rt = {}

        self.markers = {}
        self.regions = {}
        self._create_overlays()
        self._build_ui()
        self.set_controls("idle")
        self.capture_runtime()
        self._update_status()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.after(40, self._drain)

    # ---------------- 线程安全 ----------------
    def post(self, fn, *args):
        if threading.current_thread() is self._main_thread:
            try:
                fn(*args)
            except Exception:
                pass
        else:
            self._uiq.put((fn, args))

    def _drain(self):
        try:
            for _ in range(200):
                fn, args = self._uiq.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    pass
        except queue.Empty:
            pass
        except Exception:
            pass
        # 主线程单向写入快照，后台线程只读：运行中调参可实时生效且无 Tk 跨线程调用
        if self.rt:
            for k in ("poll_ms", "sample_points", "change_tol", "change_ratio",
                      "cooldown", "min_wait", "reply_stable", "reply_poll_ms",
                      "reply_timeout", "startup_quiet", "read_mode", "run_mode",
                      "guard", "preview_text", "chat_titles", "ai_titles"):
                if k in self.vars:
                    try:
                        self.rt[k] = self.vars[k].get()
                    except Exception:
                        pass
        self.root.after(40, self._drain)

    def capture_runtime(self):
        """主线程调用：冻结一份纯 Python 运行快照，供后台线程安全读取。"""
        snap = dict(self.cfg)
        for k, v in self.vars.items():
            try:
                snap[k] = v.get()
            except Exception:
                pass
        snap["_centers"] = {k: m.center() for k, m in self.markers.items()}
        snap["_rects"] = {k: r.rect() for k, r in self.regions.items()}
        self.rt = snap

    def center_of(self, key):
        return self.rt.get("_centers", {}).get(key, (0, 0))

    def rect_of(self, key):
        return self.rt.get("_rects", {}).get(key, (0, 0, 10, 10))

    def log(self, msg, level="info"):
        """任意线程可调用。内容强制脱敏，UI 更新投递到主线程。"""
        clean = sanitize(msg)
        LOGGER.write(clean)
        self.post(self._append_log, clean, level)

    def _append_log(self, clean, level):
        color = {"ok": OK, "warn": WARN, "err": ERR}.get(level, LOG_FG)
        icon = {"ok": "✔", "warn": "!", "err": "×"}.get(level, "·")
        line = "[%s] %s %s\n" % (datetime.now().strftime("%H:%M:%S"), icon, clean)
        try:
            self.logtxt.configure(state=tk.NORMAL)
            if int(self.logtxt.index("end-1c").split(".")[0]) > 400:
                self.logtxt.delete("1.0", "100.0")
            self.logtxt.insert(tk.END, line)
            self.logtxt.tag_add("c", "end-%dc" % (len(line) + 1), "end-1c")
            self.logtxt.tag_config("c", foreground=color)
            self.logtxt.see(tk.END)
            self.logtxt.configure(state=tk.DISABLED)
        except Exception:
            pass
        # 顶部状态由 set_controls 独占，日志不再覆盖它

    def describe(self, text):
        """消息展示：默认只给长度和指纹，不显示正文。"""
        if not text:
            return "空"
        preview = self.rt.get("preview_text", self.cfg.get("preview_text", False))
        if preview:
            return sanitize(text[:24])
        return "长度 %d / #%s" % (len(text), fp(text))

    # ---------------- 覆盖层 ----------------
    def _create_overlays(self):
        for key, label, color, _group in MARKER_DEFS:
            p = self.cfg["markers"].get(key, {"x": 100, "y": 100})
            self.markers[key] = Crosshair(self.root, key, label, color,
                                          p.get("x", 100), p.get("y", 100),
                                          on_release=self._sync_overlay_to_cfg)
        self.regions["monitor"] = RegionFrame(
            self.root, self.cfg["monitor_rect"], "新消息检测区", ACCENT,
            self._sync_overlay_to_cfg)
        self.regions["reply"] = RegionFrame(
            self.root, self.cfg["reply_rect"], "AI 回复检测区", "#c084fc",
            self._sync_overlay_to_cfg)

    def _sync_overlay_to_cfg(self):
        for k, m in self.markers.items():
            x, y = m.pos()
            self.cfg["markers"][k] = {"x": int(x), "y": int(y)}
        self.cfg["monitor_rect"] = self.regions["monitor"].geometry()
        self.cfg["reply_rect"] = self.regions["reply"].geometry()
        self.capture_runtime()
        self.schedule_save()

    def overlay(self, visible, wait=True):
        """主线程执行 Tk 显隐，后台线程同步等待，避免截图拍到浮层。"""
        ev = threading.Event()

        def job():
            for m in self.markers.values():
                m.set_visible(visible)
            for r in self.regions.values():
                r.set_visible(visible)
            try:
                self.root.update_idletasks()
            except Exception:
                pass
            ev.set()

        self.post(job)
        if wait:
            ev.wait(timeout=1.0)

    def ghost(self, on):
        def job():
            for m in self.markers.values():
                m.set_ghost(on)
        self.post(job)

    # ---------------- 配置读写 ----------------
    def var(self, key, kind=None):
        """按配置默认值自动选择变量类型，避免把布尔/数值误建成 StringVar。"""
        if key not in self.vars:
            val = self.cfg.get(key)
            if kind is None:
                if isinstance(val, bool):
                    kind = tk.BooleanVar
                elif isinstance(val, (int, float)):
                    kind = tk.DoubleVar
                else:
                    kind = tk.StringVar
            self.vars[key] = kind(value=val)
        return self.vars[key]

    def schedule_save(self):
        if self._save_job:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.root.after(350, self.save)

    def save(self, announce=False):
        self._save_job = None
        try:
            for k, m in self.markers.items():
                x, y = m.pos()
                self.cfg["markers"][k] = {"x": int(x), "y": int(y)}
            self.cfg["monitor_rect"] = self.regions["monitor"].geometry()
            self.cfg["reply_rect"] = self.regions["reply"].geometry()
            for k, v in self.vars.items():
                try:
                    self.cfg[k] = v.get()
                except Exception:
                    pass
            self.cfg["schema"] = 2
            LOGGER.enabled = bool(self.cfg.get("file_log"))
            self.capture_runtime()
            ok = save_config(self.cfg)
            if announce:
                self.log("设置已保存（含 %d 个定位点）" % len(self.markers),
                         "ok" if ok else "err")
        except Exception as e:
            self.log("保存设置失败：%s" % type(e).__name__, "err")

    def reset_defaults(self):
        if not messagebox.askyesno("恢复默认", "确定恢复所有设置？已摆放的定位点会重置。"):
            return
        fresh = default_config()
        fresh["screen_w"], fresh["screen_h"] = SCREEN_W, SCREEN_H
        self.cfg = fresh
        for key, m in self.markers.items():
            p = fresh["markers"][key]
            m.set_pos(p["x"], p["y"])
        self.regions["monitor"].set_geometry(fresh["monitor_rect"])
        self.regions["reply"].set_geometry(fresh["reply_rect"])
        for k, v in self.vars.items():
            if k in fresh:
                try:
                    v.set(fresh[k])
                except Exception:
                    pass
        for k, fmt in self._slider_fmts.items():
            self._slider_show(k, fmt)
        self.save(True)
        self.log("已恢复默认设置", "ok")

    # ---------------- UI ----------------
    def _build_ui(self):
        self.root.bind_all("<MouseWheel>", self._on_wheel)
        head = tk.Frame(self.root, bg=PANEL, height=62)
        head.pack(fill=tk.X, side=tk.TOP)
        head.pack_propagate(False)
        left = tk.Frame(head, bg=PANEL)
        left.pack(side=tk.LEFT, padx=16)
        tk.Label(left, text=APP_NAME, bg=PANEL, fg=FG,
                 font=("Microsoft YaHei", 14, "bold")).pack(anchor=tk.W)
        tk.Label(left, text="%s  ·  %s  ·  本地运行 / 不上传数据" % (VERSION, AUTHOR),
                 bg=PANEL, fg=MUTED, font=("Microsoft YaHei", 8)).pack(anchor=tk.W)

        self.lamp = tk.Canvas(head, width=12, height=12, bg=PANEL, highlightthickness=0)
        self.lamp.pack(side=tk.LEFT, padx=(18, 5))
        self._lamp_dot = self.lamp.create_oval(2, 2, 10, 10, fill=MUTED, outline="")
        # 常驻运行状态：不再被日志内容覆盖
        self.var_run_state = tk.StringVar(value="待机")
        self.lbl_run_state = tk.Label(head, textvariable=self.var_run_state,
                                      bg=PANEL, fg=MUTED,
                                      font=("Microsoft YaHei", 9, "bold"), anchor=tk.W)
        self.lbl_run_state.pack(side=tk.LEFT)
        # 操作反馈：按下按钮后短暂停留，明确告知用户发生了什么
        self.var_feedback = tk.StringVar(value="")
        self.lbl_feedback = tk.Label(head, textvariable=self.var_feedback,
                                     bg=PANEL, fg=WARN,
                                     font=("Microsoft YaHei", 8), anchor=tk.W)
        self.lbl_feedback.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0))

        btns = tk.Frame(head, bg=PANEL)
        btns.pack(side=tk.RIGHT, padx=12)
        self.btn_start = self._action(btns, "▶  启动", OK, self.start)
        self.btn_pause = self._action(btns, "⏸  暂停", WARN, self.pause, disabled=True)
        self.btn_stop = self._action(btns, "■  停止", ERR, self.stop, disabled=True)

        nb = ttk.Notebook(self.root)
        # 布局顺序统一放在末尾处理：先给固定高度的区域分配空间，
        # 最后才让 expand=True 的标签页占剩余空间，否则窗口较矮时状态栏会被挤出窗口。
        tabs = {}
        for name in ("快速上手", "定位校准", "识别与检测", "高级 / 隐私", "关于"):
            f = ttk.Frame(nb, style="TFrame")
            nb.add(f, text="  " + name + "  ")
            tabs[name] = scrollable(f)
        self._tab_guide(tabs["快速上手"])
        self._tab_calibrate(tabs["定位校准"])
        self._tab_detect(tabs["识别与检测"])
        self._tab_advanced(tabs["高级 / 隐私"])
        self._tab_about(tabs["关于"])

        logframe = ttk.LabelFrame(self.root, text=" 运行日志 ", padding=4)
        bar = ttk.Frame(logframe, style="Card.TFrame")
        bar.pack(fill=tk.X, pady=(0, 3))
        ttk.Button(bar, text="清空", command=self._clear_log).pack(side=tk.RIGHT)
        tk.Label(bar, text="日志只记录事件与指纹，不记录消息正文", bg=PANEL, fg=MUTED,
                 font=("Microsoft YaHei", 8)).pack(side=tk.LEFT)
        self.logtxt = tk.Text(logframe, height=6, state=tk.DISABLED, bg=LOG_BG,
                              fg=LOG_FG, font=MONO, wrap=tk.WORD,
                              relief=tk.FLAT, padx=6, pady=4)
        self.logtxt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(logframe, command=self.logtxt.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.logtxt.configure(yscrollcommand=sb.set)

        status = tk.Frame(self.root, bg=PANEL, height=26)
        status.pack_propagate(False)
        self.sb = {}
        # 右侧链接先 pack：pack 按先后顺序分配空间，先放才能避免被统计字段挤掉/截断
        linkbox = tk.Frame(status, bg=PANEL, cursor="hand2")
        linkbox.pack(side=tk.RIGHT, padx=10)
        mini = tk.Canvas(linkbox, width=16, height=16, bg=PANEL, highlightthickness=0)
        mini.pack(side=tk.LEFT, padx=(0, 5))
        mini.create_oval(1, 2, 11, 12, fill="#4f8cff", outline="")
        mini.create_oval(6, 5, 15, 14, fill="#c084fc", outline="")
        mini.create_line(12, 1, 15, 1, fill="#3ddc97", width=2)
        mini.create_line(15, 1, 15, 4, fill="#3ddc97", width=2)
        lnk = tk.Label(linkbox, text="更多有趣实用项目 ↗", bg=PANEL, fg=ACCENT,
                       font=("Microsoft YaHei", 8, "underline"), cursor="hand2")
        lnk.pack(side=tk.LEFT)
        for w in (linkbox, mini, lnk):
            w.bind("<Button-1>", lambda _e: self.open_repo())
        self._tooltip(lnk, REPO + "   （单击打开）")

        # 运行状态已在顶部专用显示，状态栏只保留统计字段，给右侧链接留足空间
        for label in ("识别方式", "已处理", "最近耗时", "上次成功"):
            tk.Label(status, text=" " + label + " ", bg=PANEL, fg=MUTED,
                     font=("Microsoft YaHei", 8)).pack(side=tk.LEFT)
            v = tk.StringVar(value="-")
            tk.Label(status, textvariable=v, bg=PANEL, fg=FG,
                     font=("Microsoft YaHei", 8, "bold"), width=10, anchor=tk.W).pack(side=tk.LEFT)
            self.sb[label] = v

        # 固定区域先分配（自下而上），标签页最后拿剩余空间
        status.pack(fill=tk.X, side=tk.BOTTOM)
        logframe.pack(fill=tk.X, side=tk.BOTTOM, padx=8, pady=(0, 4))
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 4))

    def _on_wheel(self, event):
        w = event.widget
        for _ in range(8):
            if isinstance(w, tk.Canvas):
                wheel_scroll(event, w)
                return "break"
            w = getattr(w, "master", None)
            if w is None:
                break
        return None

    def _action(self, parent, text, color, command, disabled=False):
        b = tk.Button(parent, text=text, command=command, bg=color, fg=BG,
                      activebackground=color, activeforeground=BG,
                      font=("Microsoft YaHei", 9, "bold"), relief=tk.FLAT,
                      padx=14, pady=5, cursor="hand2",
                      state=tk.DISABLED if disabled else tk.NORMAL,
                      disabledforeground=BTN_OFF_FG)
        # 按下瞬间下沉，给出明确的触觉式视觉反馈
        b.bind("<ButtonPress-1>", lambda _e: b.configure(relief=tk.SUNKEN))
        b.bind("<ButtonRelease-1>", lambda _e: b.configure(relief=tk.FLAT))
        b.pack(side=tk.LEFT, padx=3)
        return b

    def set_controls(self, mode):
        """统一切换三个按钮的文字/颜色/可用性 + 常驻运行状态文字。"""
        def style(btn, text, color, enabled):
            btn.configure(text=text,
                          state=tk.NORMAL if enabled else tk.DISABLED,
                          bg=color if enabled else BTN_OFF_BG,
                          fg=BG if enabled else BTN_OFF_FG,
                          activebackground=color if enabled else BTN_OFF_BG,
                          activeforeground=BG,
                          disabledforeground=BTN_OFF_FG)
        if mode == "running":
            style(self.btn_start, "●  运行中", OK, False)
            style(self.btn_pause, "⏸  暂停", WARN, True)
            style(self.btn_stop, "■  停止", ERR, True)
            self.var_run_state.set("▶ 监控运行中")
            self.lbl_run_state.configure(fg=OK)
        elif mode == "paused":
            style(self.btn_start, "●  已暂停", OK, False)
            style(self.btn_pause, "▶  继续", ACCENT, True)
            style(self.btn_stop, "■  停止", ERR, True)
            self.var_run_state.set("⏸ 已暂停")
            self.lbl_run_state.configure(fg=WARN)
        elif mode == "stopping":
            style(self.btn_start, "●  停止中", OK, False)
            style(self.btn_pause, "⏸  暂停", WARN, False)
            style(self.btn_stop, "■  正在停止…", ERR, False)
            self.var_run_state.set("■ 正在停止")
            self.lbl_run_state.configure(fg=WARN)
        else:
            style(self.btn_start, "▶  启动", OK, True)
            style(self.btn_pause, "⏸  暂停", WARN, False)
            style(self.btn_stop, "■  停止", ERR, False)
            self.var_run_state.set("待机")
            self.lbl_run_state.configure(fg=MUTED)
        self._update_status()

    def _notify(self, text, color=WARN):
        """顶部短暂操作反馈，2.6 秒后自动消失。"""
        self.var_feedback.set("· " + text)
        self.lbl_feedback.configure(fg=color)
        if getattr(self, "_feedback_job", None):
            try:
                self.root.after_cancel(self._feedback_job)
            except Exception:
                pass
        self._feedback_job = self.root.after(2600,
                                             lambda: self.var_feedback.set(""))

    def _tooltip(self, widget, text):
        holder = {"w": None}

        def show(_e=None):
            if holder["w"]:
                return
            t = tk.Toplevel(widget)
            t.overrideredirect(True)
            t.attributes("-topmost", True)
            tk.Label(t, text=text, bg=CARD, fg=FG,
                     font=("Microsoft YaHei", 8), padx=6, pady=3).pack()
            t.geometry("+%d+%d" % (widget.winfo_rootx(),
                                   widget.winfo_rooty() - 24))
            holder["w"] = t

        def hide(_e=None):
            if holder["w"]:
                try:
                    holder["w"].destroy()
                except Exception:
                    pass
                holder["w"] = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    def open_repo(self):
        try:
            os.startfile(REPO)
        except Exception:
            try:
                import webbrowser
                webbrowser.open(REPO)
            except Exception:
                self.log("无法打开浏览器", "warn")
                return
        self._notify("已打开项目主页", ACCENT)

    def _card(self, parent, title):
        box = ttk.LabelFrame(parent, text=" " + title + " ", padding=8)
        box.pack(fill=tk.X, padx=10, pady=5)
        body = ttk.Frame(box, style="Card.TFrame")
        body.pack(fill=tk.X)
        return body

    def _note(self, parent, text, color=MUTED):
        lb = tk.Label(parent, text=text, bg=PANEL, fg=color,
                      font=("Microsoft YaHei", 8), wraplength=760,
                      justify=tk.LEFT, anchor=tk.W)
        lb.pack(fill=tk.X, pady=(2, 0))
        return lb

    def legal_block(self, parent, compact=False):
        """醒目的合规声明 + 账号风险警告（快速上手用精简版，关于页用完整版）。"""
        box = tk.Frame(parent, bg=PANEL, highlightbackground=ERR,
                       highlightthickness=1)
        box.pack(fill=tk.X, padx=10, pady=7)
        tk.Label(box, text="  ⚠  合法用途声明与账号风险警告", bg=PANEL, fg=ERR,
                 font=("Microsoft YaHei", 10, "bold"), anchor=tk.W).pack(
                     fill=tk.X, padx=8, pady=(6, 4))
        legal = LEGAL_LINES[:1] if compact else LEGAL_LINES
        risk = RISK_LINES[:2] if compact else RISK_LINES
        for t in legal:
            self._note(box, "· " + t, FG)
        tk.Label(box, text="  ▸ 账号风险", bg=PANEL, fg=WARN,
                 font=("Microsoft YaHei", 9, "bold"), anchor=tk.W).pack(
                     fill=tk.X, padx=4, pady=(6, 2))
        for t in risk:
            self._note(box, "· " + t, WARN if compact else FG)
        if compact:
            self._note(box, "完整声明请见「关于」页。使用前请务必阅读。", ERR)
        else:
            self._note(box, "请审慎评估风险后再决定是否使用。继续即视为已知悉并接受上述条款。", ERR)
        tk.Frame(box, bg=PANEL, height=5).pack(fill=tk.X)
        return box

    def _slider(self, parent, key, label, lo, hi, step, fmt, note, row):
        # 滑块统一放进独立的 grid 容器，避免与卡片内 pack 管理的说明文字冲突
        if id(parent) not in self._slider_frames:
            holder = ttk.Frame(parent, style="Card.TFrame")
            holder.pack(fill=tk.X)
            self._slider_frames[id(parent)] = holder
        g = self._slider_frames[id(parent)]
        ttk.Label(g, text=label, width=15, style="Card.TLabel").grid(
            row=row, column=0, sticky=tk.W, pady=3)
        v = self.var(key, tk.DoubleVar)
        sc = ttk.Scale(g, from_=lo, to=hi, variable=v, orient=tk.HORIZONTAL,
                       length=250,
                       command=lambda _x, k=key, f=fmt: (self._slider_show(k, f),
                                                         self.schedule_save()))
        sc.grid(row=row, column=1, sticky=tk.W, padx=6)
        lab = tk.Label(g, text="", bg=PANEL, fg=ACCENT, font=MONO, width=12, anchor=tk.W)
        lab.grid(row=row, column=2, sticky=tk.W)
        self.slider_labels[key] = lab
        self._slider_fmts[key] = fmt
        ttk.Label(g, text=note, style="Card.Muted.TLabel", wraplength=470,
                  justify=tk.LEFT).grid(row=row, column=3, sticky=tk.W, padx=8)
        self._slider_show(key, fmt)
        sc.bind("<ButtonRelease-1>", lambda _e: self.schedule_save())
        g.columnconfigure(3, weight=1)

    def _slider_show(self, key, fmt):
        lab = self.slider_labels.get(key)
        if lab is None:
            return
        try:
            lab.configure(text=fmt(float(self.vars[key].get())))
        except Exception:
            pass

    def _check(self, parent, key, text, note=""):
        v = self.var(key, tk.BooleanVar)
        ttk.Checkbutton(parent, text=text, variable=v,
                        command=self.schedule_save).pack(anchor=tk.W, pady=1)
        if note:
            self._note(parent, note)
        return v

    def _tab_guide(self, f):
        box = self._card(f, "运行流程（重构后的状态机）")
        steps = [
            ("01", "选定窗口", "在「高级 / 隐私」中添加聊天窗口与 AI 窗口白名单，可选开启点击前校验。"),
            ("02", "摆放定位点", "在「定位校准」把 7 个准星拖到对应位置，并框出「新消息检测区」「AI 回复检测区」。"),
            ("03", "分步验证", "用下面的单步测试逐个验证每个环节，确认无误再启动，避免误发。"),
            ("04", "启动监控", "检测区画面变化 → 读取新消息 → 转发 AI → 回复区变化并稳定 → 复制回复 → 回发。"),
            ("05", "随时停止", "暂停 / 停止立即生效；剪贴板可自动还原，原始消息不写入磁盘。"),
        ]
        for no, title, desc in steps:
            row = tk.Frame(box, bg=PANEL)
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=no, bg=ACCENT, fg=BG, width=4,
                     font=("Consolas", 10, "bold")).pack(side=tk.LEFT, padx=(0, 8))
            col = tk.Frame(row, bg=PANEL)
            col.pack(side=tk.LEFT, fill=tk.X)
            tk.Label(col, text=title, bg=PANEL, fg=FG,
                     font=("Microsoft YaHei", 9, "bold")).pack(anchor=tk.W)
            tk.Label(col, text=desc, bg=PANEL, fg=MUTED, font=("Microsoft YaHei", 8),
                     wraplength=730, justify=tk.LEFT, anchor=tk.W).pack(anchor=tk.W)

        box = self._card(f, "运行模式")
        modes = [("both", "双向转发（消息 → AI → 回复 → 聊天）"),
                 ("forward", "仅转发到 AI（不回发）"),
                 ("detect", "仅检测（不点击、不发送，用于校准）")]
        v = self.var("run_mode")
        for val, txt in modes:
            ttk.Radiobutton(box, text=txt, variable=v, value=val,
                            command=self.schedule_save).pack(anchor=tk.W, pady=1)
        self._note(box, "「仅检测」不会执行任何鼠标键盘操作，可安全用来验证检测区是否摆对。")

        box = self._card(f, "单步测试（后台执行，互不依赖）")
        grid = ttk.Frame(box, style="Card.TFrame")
        grid.pack(fill=tk.X)
        tests = [("① 读取新消息", self.test_read),
                 ("② 转发到 AI", self.test_forward),
                 ("③ 等待并取回复", self.test_reply),
                 ("④ 回发到聊天", self.test_send_back),
                 ("⑤ 显示/隐藏定位层", self.toggle_overlay)]
        for i, (txt, cmd) in enumerate(tests):
            ttk.Button(grid, text=txt, command=cmd).grid(
                row=i // 3, column=i % 3, padx=4, pady=4, sticky=tk.W)
        self._note(box, "每一步的耗时、长度与指纹会写入日志；默认不显示、不保存消息正文。")
        self._note(box, "提示：先打开聊天软件与 AI 客户端，并把它们的窗口保持在可见位置。", WARN)
        self.legal_block(f, compact=True)

    def _tab_calibrate(self, f):
        box = self._card(f, "定位点（双击任一步骤可把准星唤回到鼠标旁）")
        self._note(box, "找不到准星时：把鼠标移到想放置的大致位置，再双击下面对应步骤 —— "
                        "准星会立刻跳到鼠标指针旁并短暂高亮，之后拖动微调即可。", OK)
        head = tk.Frame(box, bg=PANEL)
        head.pack(fill=tk.X, pady=(4, 0))
        for col, txt in enumerate(("步骤", "作用", "所属窗口", "提示")):
            tk.Label(head, text=txt, bg=PANEL, fg=MUTED,
                     font=("Microsoft YaHei", 8, "bold"), width=26 if col == 3 else 18,
                     anchor=tk.W).pack(side=tk.LEFT)
        tips = {
            "chat_msg": "对准最新一条消息，程序在此处右键",
            "copy_item": "右键菜单弹出后「复制」项的位置",
            "ai_input": "AI 客户端的文本输入框",
            "ai_send": "AI 客户端的发送按钮",
            "ai_copy_btn": "AI 回复下方的「复制」按钮",
            "chat_input": "聊天窗口的输入框",
            "chat_send": "聊天窗口的发送按钮",
        }
        for key, label, color, group in MARKER_DEFS:
            row = tk.Frame(box, bg=PANEL, cursor="hand2")
            row.pack(fill=tk.X, pady=1)
            widgets = [row]
            a = tk.Label(row, text=" " + label, bg=color, fg=BG, width=24,
                         font=("Microsoft YaHei", 8, "bold"), anchor=tk.W)
            a.pack(side=tk.LEFT, padx=(0, 4))
            widgets.append(a)
            b = tk.Label(row, text=("聊天软件" if group == "chat" else "AI 客户端"),
                         bg=PANEL, fg=FG, width=16, anchor=tk.W,
                         font=("Microsoft YaHei", 8))
            b.pack(side=tk.LEFT)
            widgets.append(b)
            c = tk.Label(row, text=tips[key], bg=PANEL, fg=MUTED,
                         font=("Microsoft YaHei", 8), anchor=tk.W)
            c.pack(side=tk.LEFT)
            widgets.append(c)
            # 行内任意位置双击都能唤回（子控件不冒泡，需逐个绑定）
            for w in widgets:
                w.bind("<Double-Button-1>",
                       lambda _e, k=key: self.locate_marker(k))

        box = self._card(f, "检测区域（拖动左上角标签移动，右下角 ↘ 调整大小）")
        self._note(box, "蓝色「新消息检测区」：框住聊天消息列表，画面变化即认为可能来了新消息。")
        self._note(box, "紫色「AI 回复检测区」：框住 AI 回复正文。程序等待该区域先变化、再连续稳定，"
                        "才判定回复完成，避免复制到上一轮的旧回复。")
        self._note(box, "两个区域都不要覆盖准星以外的固定工具栏，否则动画会造成误触发。", WARN)

        box = self._card(f, "定位层操作")
        grid = ttk.Frame(box, style="Card.TFrame")
        grid.pack(fill=tk.X)
        for i, (txt, cmd) in enumerate([
            ("显示 / 隐藏", self.toggle_overlay),
            ("保存位置", lambda: self.save(True)),
            ("重置定位点", self.reset_defaults),
            ("显示所有提示", self.show_tips),
            ("蓝框移到鼠标", lambda: self.locate_region("monitor")),
            ("紫框移到鼠标", lambda: self.locate_region("reply")),
        ]):
            ttk.Button(grid, text=txt, command=cmd).grid(
                row=i // 3, column=i % 3, padx=4, pady=3, sticky=tk.W)
        self._note(box, "检测框也可以用上面两个按钮唤回到鼠标位置；"
                        "拖动结束后 300ms 自动保存，准星以屏幕绝对坐标记录，重启不会漂移。")

    def _tab_detect(self, f):
        box = self._card(f, "消息读取方式")
        v = self.var("read_mode")
        for val, txt in [("clipboard", "复制粘贴（通用，推荐）"),
                         ("uia", "界面控件识别（需 uiautomation）"),
                         ("ocr", "OCR 图像识别（需可选 OCR 组件）")]:
            ttk.Radiobutton(box, text=txt, variable=v, value=val,
                            command=self.schedule_save).pack(anchor=tk.W, pady=1)
        status = "可用" if HAS_UIA else "不可用（未安装）"
        self._note(box, "界面控件识别：%s　｜　OCR：%s" % (status, OCR.state))
        self._note(box, "复制粘贴模式通过右键菜单复制消息，兼容绝大多数聊天客户端；"
                        "其他模式失败时会自动回退到复制粘贴。")

        box = self._card(f, "检测参数")
        self._slider(box, "poll_ms", "轮询间隔", 150, 1500, 50,
                     lambda v: "%d ms" % int(v), "越小响应越快，CPU 占用越高", 0)
        self._slider(box, "sample_points", "采样点数", 36, 400, 4,
                     lambda v: "%d 点" % int(v), "网格采样点数，越多越灵敏", 1)
        self._slider(box, "change_tol", "像素容差", 4, 60, 2,
                     lambda v: "%d" % int(v), "低于此色差视为闪烁噪声，忽略", 2)
        self._slider(box, "change_ratio", "变化阈值", 0.01, 0.30, 0.01,
                     lambda v: "%.0f%%" % (v * 100), "采样点中变化占比超过此值才触发", 3)
        self._slider(box, "cooldown", "发送冷却", 0.0, 5.0, 0.1,
                     lambda v: "%.1f s" % v, "两轮转发之间的最小间隔", 4)
        self._slider(box, "startup_quiet", "启动静默", 0.0, 6.0, 0.5,
                     lambda v: "%.1f s" % v, "启动后这段时间内只校准基准，不转发", 5)

        box = self._card(f, "回复判定（变化 → 稳定 状态机）")
        self._slider(box, "min_wait", "最短等待", 0.0, 10.0, 0.5,
                     lambda v: "%.1f s" % v, "发送后至少等待，避免读到旧回复", 0)
        self._slider(box, "reply_stable", "稳定时长", 0.4, 5.0, 0.2,
                     lambda v: "%.1f s" % v, "回复区连续不变达到此时长才判定完成", 1)
        self._slider(box, "reply_poll_ms", "回复轮询", 150, 1200, 50,
                     lambda v: "%d ms" % int(v), "回复检测采样间隔", 2)
        self._slider(box, "reply_timeout", "等待上限", 30, 600, 10,
                     lambda v: "%d s" % int(v), "超时后放弃本轮，不会误发旧内容", 3)
        self._note(box, "只有「回复区确实出现过变化」且随后稳定，才会点击复制按钮；"
                        "这是与旧版按按钮像素相似度判定最大的区别。", OK)

    def _tab_advanced(self, f):
        box = self._card(f, "目标窗口白名单（可选）")
        rg = ttk.Frame(box, style="Card.TFrame")
        rg.pack(fill=tk.X)
        for i, (label, key, kind) in enumerate([
            ("聊天软件窗口", "chat_titles", "聊天软件"),
            ("AI 客户端窗口", "ai_titles", "AI"),
        ]):
            ttk.Label(rg, text=label, width=16, style="Card.TLabel").grid(row=i, column=0, sticky=tk.W, pady=2)
            ttk.Entry(rg, textvariable=self.var(key), state="readonly").grid(row=i, column=1, sticky=tk.EW, padx=4)
            ttk.Button(rg, text="添加", command=lambda k=key, n=kind: self.pick_windows(k, n)).grid(row=i, column=2, padx=2)
            ttk.Button(rg, text="清空", command=lambda k=key: (self.var(k).set(""), self.schedule_save())).grid(row=i, column=3, padx=2)
        rg.columnconfigure(1, weight=1)
        self._note(box, "默认目标窗口为空：程序不预设、不自动填入任何窗口。"
                        "留空即只靠准星定位、不做窗口校验；需要时点「添加」手动勾选。", OK)
        self._check(box, "guard", "启用窗口白名单校验",
                    "点击前检查该坐标所属窗口标题是否在白名单内；第三方弹窗遮挡时会拦截，避免误点。")
        self._note(box, "窗口标题仅保存在本机配置文件中，日志里只会显示不可逆指纹，不会写入原文。")

        box = self._card(f, "点击与剪贴板")
        click = ttk.Frame(box, style="Card.TFrame")
        click.pack(fill=tk.X)
        ttk.Label(click, text="点击方式", width=16, style="Card.TLabel").pack(side=tk.LEFT)
        v = self.var("click_method")
        for val, txt in [("cursor", "光标移动"), ("postmsg", "后台注入"), ("sendmsg", "强制注入")]:
            ttk.Radiobutton(click, text=txt, variable=v, value=val,
                            command=self.schedule_save).pack(side=tk.LEFT, padx=5)
        self._note(box, "光标移动兼容性最好；后台/强制注入不移动鼠标，但部分客户端不响应。")
        self._check(box, "jitter", "点击坐标轻微随机偏移（±2 像素）")
        self._check(box, "clipboard_restore", "任务结束后恢复原有剪贴板内容")
        self._check(box, "rescale", "分辨率变化时按比例缩放定位点（默认关闭，严格保留位置）")

        box = self._card(f, "隐私")
        self._check(box, "preview_text", "日志中显示消息预览（默认关闭，仅显示长度与指纹）")
        self._check(box, "file_log", "写入本地日志文件（默认关闭；写入时仍强制脱敏）")
        self._note(box, "程序不联网、不上传、不收集任何消息内容；消息只在本机剪贴板与内存之间流转。", OK)
        self._note(box, "配置文件与日志位于当前用户的漫游应用数据目录下，不随程序分发。")

        box = self._card(f, "维护")
        grid = ttk.Frame(box, style="Card.TFrame")
        grid.pack(fill=tk.X)
        ttk.Button(grid, text="保存设置", command=lambda: self.save(True)).grid(row=0, column=0, padx=4)
        ttk.Button(grid, text="恢复默认", command=self.reset_defaults).grid(row=0, column=1, padx=4)
        ttk.Button(grid, text="检测组件状态", command=self.check_components).grid(row=0, column=2, padx=4)
        ttk.Button(grid, text="打开配置目录", command=self.open_config_dir).grid(row=0, column=3, padx=4)

    def _tab_about(self, f):
        self.legal_block(f, compact=False)

        box = self._card(f, APP_NAME)
        for k, v in [("版本", VERSION), ("发布日期", BUILD_DATE),
                     ("作者", AUTHOR),
                     ("运行环境", "Windows 10 / 11 · 64 位"),
                     ("依赖", "核心零第三方依赖；UI Automation / OCR 为可选增强")]:
            row = tk.Frame(box, bg=PANEL)
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=k, bg=PANEL, fg=MUTED, width=12,
                     anchor=tk.W, font=("Microsoft YaHei", 8)).pack(side=tk.LEFT)
            tk.Label(row, text=v, bg=PANEL, fg=FG, anchor=tk.W,
                     font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        row = tk.Frame(box, bg=PANEL)
        row.pack(fill=tk.X, pady=1)
        tk.Label(row, text="仓库", bg=PANEL, fg=MUTED, width=12,
                 anchor=tk.W, font=("Microsoft YaHei", 8)).pack(side=tk.LEFT)
        lnk = tk.Label(row, text=REPO + "  ↗", bg=PANEL, fg=ACCENT,
                       cursor="hand2", anchor=tk.W,
                       font=("Microsoft YaHei", 9, "underline"))
        lnk.pack(side=tk.LEFT)
        lnk.bind("<Button-1>", lambda _e: self.open_repo())
        self._tooltip(lnk, "单击在浏览器中打开")
        self._note(box, "更多有趣实用项目都在里面，欢迎 Star ⭐", OK)

        box = self._card(f, "更新记录")
        for ver, desc in CHANGELOG:
            row = tk.Frame(box, bg=PANEL)
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=ver, bg=PANEL, fg=ACCENT, width=10,
                     anchor=tk.W, font=MONO).pack(side=tk.LEFT)
            tk.Label(row, text=desc, bg=PANEL, fg=FG, wraplength=740,
                     justify=tk.LEFT, anchor=tk.W,
                     font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, fill=tk.X)
        box = self._card(f, "隐私与免责")
        self._note(box, "· 全部识别、截图与转发都在本机完成，程序没有任何网络请求。")
        self._note(box, "· 日志默认不落盘；落盘时也不会出现消息正文、窗口标题或本机文件路径。")
        self._note(box, "· 分发包不包含任何用户配置、日志、截图或历史脚本。")
        self._note(box, "· 本工具模拟人工操作，仅可用于合法用途；账号风险与责任边界详见上方声明。", WARN)

    # ---------------- 小工具 ----------------
    def _clear_log(self):
        try:
            self.logtxt.configure(state=tk.NORMAL)
            self.logtxt.delete("1.0", tk.END)
            self.logtxt.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _update_status(self):
        def job():
            color = OK if self.running and not self.paused else (WARN if self.paused else MUTED)
            try:
                self.lamp.itemconfig(self._lamp_dot, fill=color)
            except Exception:
                pass
            self.sb["识别方式"].set(self.last_mode)
            self.sb["已处理"].set(str(self.msg_count))
            self.sb["最近耗时"].set("%.0f ms" % self.last_ms if self.last_ms else "-")
            self.sb["上次成功"].set(self.last_ok)
        self.post(job)

    def toggle_overlay(self):
        self.last_overlay_state = not self.last_overlay_state
        self.overlay(self.last_overlay_state, wait=False)
        self.log("定位层已%s" % ("显示" if self.last_overlay_state else "隐藏"))

    def show_tips(self):
        for m in self.markers.values():
            m._show_tip()
        self.root.after(2500, lambda: [m._hide_tip() for m in self.markers.values()])

    def locate_marker(self, key):
        """双击步骤行：把对应准星移到鼠标指针旁并短暂高亮。"""
        try:
            m = self.markers.get(key)
            if not m:
                return
            px, py = self.root.winfo_pointerxy()
            half = m.SIZE // 2
            # 钳制到可见屏幕范围：即使指针在边缘/副屏异常坐标，准星也一定找得到
            cx = max(half, min(SCREEN_W - half, px + 30))
            cy = max(half, min(SCREEN_H - half, py + 30))
            m.set_pos(cx - half, cy - half)
            m.flash()
            self.last_overlay_state = True
            self.capture_runtime()
            self.schedule_save()
            self._notify("已把「%s」移到鼠标旁，拖动可微调" % m.label, ACCENT)
            self.log("定位点 %s 已唤回到鼠标指针旁" % key, "ok")
        except Exception:
            self.log("移动定位点失败", "err")

    def locate_region(self, key):
        """把检测框左上角移到鼠标位置，尺寸保持不变。"""
        try:
            r = self.regions.get(key)
            if not r:
                return
            px, py = self.root.winfo_pointerxy()
            g = r.geometry()
            x = max(0, min(SCREEN_W - g[2], px - 18))
            y = max(0, min(SCREEN_H - g[3], py - 18))
            r.set_geometry([x, y, g[2], g[3]])
            self.last_overlay_state = True
            self.capture_runtime()
            self.schedule_save()
            self._notify("检测框已移到鼠标位置", ACCENT)
            self.log("检测区 %s 已移到鼠标位置" % key, "ok")
        except Exception:
            self.log("移动检测框失败", "err")

    def pick_windows(self, key, kind):
        titles = []

        def cb(hwnd, _lp):
            try:
                if user32.IsWindowVisible(hwnd):
                    t = window_title(hwnd)
                    if t and len(t) < 200:
                        titles.append(t)
            except Exception:
                pass
            return True

        proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb)
        try:
            user32.EnumWindows(proc, 0)
        except Exception:
            pass
        if not titles:
            self.log("未枚举到可见窗口", "warn")
            return
        current = [s.strip() for s in re.split(r"[,;\uFF0C\uFF1B]+", self.var(key).get() or "") if s.strip()]
        top = tk.Toplevel(self.root)
        top.title("勾选要加入%s白名单的窗口" % kind)
        if APP_ICON:
            try:
                top.iconbitmap(default=APP_ICON)
            except Exception:
                pass
        top.configure(bg=BG)
        top.geometry("520x420")
        tk.Label(top, text="默认为空、不预设任何窗口；只有勾选的才会加入。"
                          "窗口标题只保存在本机，不会写入日志。",
                 bg=BG, fg=MUTED, wraplength=490, justify=tk.LEFT,
                 font=("Microsoft YaHei", 8)).pack(anchor=tk.W, padx=8, pady=6)
        holder = ttk.Frame(top, style="TFrame")
        holder.pack(fill=tk.BOTH, expand=True, padx=8)
        cv = tk.Canvas(holder, bg=PANEL, highlightthickness=0)
        sb = ttk.Scrollbar(holder, command=cv.yview)
        cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        cv.configure(yscrollcommand=sb.set)
        inner = ttk.Frame(cv, style="Card.TFrame")
        cv.create_window((0, 0), window=inner, anchor=tk.NW)
        inner.bind("<Configure>", lambda _e: cv.configure(scrollregion=cv.bbox("all")))
        sel = {}
        for t in sorted(set(titles)):
            v = tk.BooleanVar(value=(t in current))
            sel[t] = v
            ttk.Checkbutton(inner, text=" " + t[:70], variable=v).pack(anchor=tk.W)

        def ok():
            merged = list(current)
            for t, v in sel.items():
                if v.get() and t not in merged:
                    merged.append(t)
            self.var(key).set("；".join(merged))
            self.schedule_save()
            self.log("白名单已更新：共 %d 个窗口标题" % len(merged), "ok")
            top.destroy()

        def clear_all():
            for v in sel.values():
                v.set(False)
            self.var(key).set("")
            self.schedule_save()
            self.log("白名单已清空（恢复默认：无目标窗口）", "ok")
            top.destroy()

        bar = ttk.Frame(top, style="TFrame")
        bar.pack(fill=tk.X, padx=8, pady=6)
        ttk.Button(bar, text="确定", command=ok).pack(side=tk.LEFT, padx=3)
        ttk.Button(bar, text="清空并关闭", command=clear_all).pack(side=tk.LEFT, padx=3)
        ttk.Button(bar, text="取消", command=top.destroy).pack(side=tk.LEFT, padx=3)

    def check_components(self):
        self.log("组件：GDI 抓图=内置 / 剪贴板=Win32 原生 / numpy=%s" %
                 ("加速" if HAS_NUMPY else "未安装(使用标准库)"))
        self.log("组件：UI Automation=%s / OCR=%s" %
                 ("可用" if HAS_UIA else "未安装", OCR.state))
        self.log("屏幕：%dx%d，配置目录位于当前用户应用数据目录" % (SCREEN_W, SCREEN_H))

    def open_config_dir(self):
        try:
            os.startfile(USER_DIR)  # noqa: S606 - 打开本机配置目录，由用户主动触发
        except Exception:
            self.log("无法打开配置目录", "warn")

    # ---------------- 白名单校验 ----------------
    def _kws(self, group):
        key = "chat_titles" if group == "chat" else "ai_titles"
        raw = self.rt.get(key, self.cfg.get(key, ""))
        return [s.strip() for s in re.split(r"[,;\uFF0C\uFF1B]+", str(raw or "")) if s.strip()]

    def _title_match(self, hwnd, kws):
        if not hwnd or not kws:
            return True
        t = window_title(hwnd)
        if not t:
            return True  # 系统菜单 / 无标题浮层放行
        return any(k.lower() in t.lower() for k in kws)

    def guard_ok(self, key):
        try:
            if not self.rt.get("guard", False):
                return True
            group = "chat" if key in ("chat_msg", "copy_item", "chat_input", "chat_send") else "ai"
            if key == "copy_item":
                return True  # 右键菜单是独立弹出窗口
            kws = self._kws(group)
            if not kws:
                return True
            x, y = self.center_of(key)
            h = top_window(x, y)
            if self._title_match(h, kws):
                self._guard_cache[group] = h
                return True
            return self._title_match(self._guard_cache[group], kws)
        except Exception:
            return True

    def guard_hwnd(self, hwnd):
        try:
            if not self.rt.get("guard", False):
                return True
            kws = self._kws("chat") + self._kws("ai")
            if not kws:
                return True
            if self._title_match(hwnd, kws):
                return True
            return any(self._title_match(self._guard_cache[g], kws) for g in ("chat", "ai"))
        except Exception:
            return True

    # ---------------- 流程原语 ----------------
    def click_marker(self, key, btn="left", hide=True):
        if not self.guard_ok(key):
            self.log("窗口校验拦截：定位点 %s 处不是目标窗口" % key, "warn")
            return 0
        x, y = self.center_of(key)
        if hide:
            self.overlay(False)
        try:
            hwnd = do_click(x, y, btn)
        finally:
            if hide:
                self.overlay(True)
        return hwnd

    def paste_text(self, text, hwnd, send=False):
        if not self.guard_hwnd(hwnd):
            self.log("窗口校验拦截：拒绝粘贴到非白名单窗口", "warn")
            return False
        self.clipguard.snapshot()
        if not clip_set(text):
            self.log("写入剪贴板失败", "err")
            return False
        activate(hwnd)
        time.sleep(0.08)
        ctrl_v()
        time.sleep(0.10)
        if send:
            press_enter()
        return True

    def read_message(self):
        """读取聊天窗口最新消息：优先选定方式，失败自动回退到复制粘贴。"""
        mode = self.rt.get("read_mode", "clipboard")
        self.last_mode = {"clipboard": "复制粘贴", "uia": "界面控件", "ocr": "OCR"}.get(mode, "复制粘贴")
        if mode == "uia" and HAS_UIA:
            text = self._read_via_uia(self._kws("chat"))
            if text:
                return text
            self.log("界面控件读取失败，回退复制粘贴", "warn")
        elif mode == "ocr":
            text = OCR.read(self.rect_of("monitor"), 0.5)
            if text:
                self.last_ms = OCR.last_ms
                return text
            self.log("OCR 未读到文本，回退复制粘贴", "warn")
        return self._read_via_clipboard()

    def _read_via_clipboard(self):
        before = clip_get()
        self.click_marker("chat_msg", "right")
        time.sleep(0.22)
        self.click_marker("copy_item", "left")
        for _ in range(6):
            time.sleep(0.09)
            text = clip_get().strip()
            if text and text != before.strip():
                return text
        return ""

    @staticmethod
    def _read_via_uia(kws):
        if not HAS_UIA or not kws:
            return ""
        try:
            import uiautomation as auto
            win = auto.WindowControl(searchDepth=2, SubName=kws[0])
            if not win.Exists(0.3, 0.05):
                return ""
            texts = []
            for c in win.GetChildrenControl(searchDepth=12):
                try:
                    if c.ControlType in (auto.ControlType.TextControl,
                                         auto.ControlType.ListItemControl):
                        name = (c.Name or "").strip()
                        if name:
                            texts.append(name)
                except Exception:
                    continue
                if len(texts) >= 60:
                    break
            return texts[-1] if texts else ""
        except Exception:
            return ""

    def forward_to_ai(self, text):
        h = self.click_marker("ai_input")
        if not self.paste_text(text, h):
            return False
        time.sleep(0.08)
        self.click_marker("ai_send")
        return True

    def wait_reply_ready(self):
        """状态机：回复区必须出现变化，随后连续稳定，才判定生成完成。"""
        rect = self.rect_of("reply")
        pts = int(self.rt.get("sample_points", 144))
        tol = int(self.rt.get("change_tol", 18))
        eps = float(self.rt.get("change_ratio", 0.04))
        min_wait = float(self.rt.get("min_wait", 2.0))
        stable = float(self.rt.get("reply_stable", 1.2))
        timeout = float(self.rt.get("reply_timeout", 180))
        poll = max(0.12, float(self.rt.get("reply_poll_ms", 400)) / 1000.0)
        t0 = time.monotonic()
        prev = None
        seen_change = False
        last_change = t0
        self.log("等待 AI 回复（变化 → 稳定）")
        while self.running and not self.paused:
            now = time.monotonic()
            if now - t0 > timeout:
                self.log("等待回复超时，本轮放弃", "warn")
                return False
            cur = snapshot(rect, pts)
            if prev is not None and cur is not None:
                if cur[0] != prev[0] and changed_ratio(prev[1], cur[1], tol) >= eps:
                    seen_change = True
                    last_change = now
            prev = cur
            if seen_change and (now - last_change) >= stable and (now - t0) >= min_wait:
                self.last_ms = (now - t0) * 1000
                self.log("回复区已稳定 %.1fs，判定生成完成" % (now - t0), "ok")
                return True
            time.sleep(poll)
        return False

    def read_reply(self):
        """优先点击 AI 复制按钮；未配置时回退到 OCR 读取回复区文本。"""
        if self.guard_ok("ai_copy_btn"):
            before = clip_get()
            cx, cy = self.center_of("ai_copy_btn")
            self.overlay(False)
            try:
                do_click(cx, cy, "left")
            finally:
                self.overlay(True)
            for _ in range(6):
                time.sleep(0.09)
                text = clip_get().strip()
                if text and text != before.strip():
                    return text
        return OCR.read(self.rect_of("reply"), 0.5)

    def send_back(self, text):
        h = self.click_marker("chat_input")
        if not self.paste_text(text, h):
            return False
        time.sleep(0.08)
        self.click_marker("chat_send")
        return True

    # ---------------- 单步测试 ----------------
    def _async(self, fn, name):
        if self.worker and self.worker.is_alive():
            self.log("已有任务在执行，请先停止", "warn")
            return
        self.running = True
        self.clipguard.saved = None
        self.capture_runtime()

        def wrap():
            try:
                fn()
            except Exception as e:
                self.last_err = type(e).__name__
                self.log("%s 失败：%s" % (name, type(e).__name__), "err")
            finally:
                self.running = False
                self.post(self._update_status)
                self.clipguard.restore()

        self.worker = threading.Thread(target=wrap, daemon=True)
        self.worker.start()
        self._update_status()

    def test_read(self):
        self._async(lambda: self._do_test_read(), "读取测试")

    def _do_test_read(self):
        self.log("单步测试：读取新消息")
        text = self.read_message()
        self.log("读取结果：%s" % self.describe(text), "ok" if text else "warn")

    def test_forward(self):
        def job():
            self.log("单步测试：转发到 AI")
            text = clip_get().strip()
            if not text:
                text = self._read_via_clipboard()
            if not text:
                self.log("剪贴板为空且未能读取消息，请先执行 ①", "warn")
                return
            self.log("待转发：%s" % self.describe(text))
            self.log("转发%s" % ("成功" if self.forward_to_ai(text) else "失败"),
                     "ok" if text else "err")
        self._async(job, "转发测试")

    def test_reply(self):
        def job():
            self.log("单步测试：等待并取回复")
            if not self._simulate_wait():
                return
            text = self.read_reply()
            self.log("回复结果：%s" % self.describe(text), "ok" if text else "warn")
        self._async(job, "回复测试")

    def test_send_back(self):
        def job():
            self.log("单步测试：回发到聊天")
            text = clip_get().strip()
            if not text:
                self.log("剪贴板为空，请先执行 ③", "warn")
                return
            self.log("待回发：%s" % self.describe(text))
            self.log("回发%s" % ("成功" if self.send_back(text) else "失败"), "ok")
        self._async(job, "回发测试")

    def _simulate_wait(self):
        """单步测试时也要求真实的变化→稳定过程，但缩短超时以免久等。"""
        rect = self.rect_of("reply")
        pts = int(self.rt.get("sample_points", 144))
        tol = int(self.rt.get("change_tol", 18))
        eps = float(self.rt.get("change_ratio", 0.04))
        stable = float(self.rt.get("reply_stable", 1.2))
        poll = max(0.12, float(self.rt.get("reply_poll_ms", 400)) / 1000.0)
        t0 = time.monotonic()
        prev, seen, last = None, False, t0
        while self.running and time.monotonic() - t0 < 60:
            cur = snapshot(rect, pts)
            if prev is not None and cur is not None and cur[0] != prev[0] \
                    and changed_ratio(prev[1], cur[1], tol) >= eps:
                seen, last = True, time.monotonic()
            prev = cur
            if seen and time.monotonic() - last >= stable:
                self.log("回复区已稳定", "ok")
                return True
            time.sleep(poll)
        self.log("未观察到回复变化或超时", "warn")
        return False

    # ---------------- 主循环 ----------------
    def start(self):
        if self.running:
            return
        global _click_method, _click_jitter
        _click_method = self.var("click_method").get()
        _click_jitter = bool(self.var("jitter").get())
        self.save()
        self.capture_runtime()
        self.clipguard.enabled = bool(self.var("clipboard_restore").get())
        self.clipguard.saved = None
        self.seen.clear()
        self.last_reply_hash = ""
        self.last_err = "-"
        self.msg_count = 0
        self.last_ok = "-"
        self.log("启动监控", "ok")
        self.running = True
        self.paused = False
        self.set_controls("running")
        self._notify("已启动 · 正在校准检测基准", OK)
        self.ghost(True)
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    def pause(self):
        if not self.running:
            return
        self.paused = not self.paused
        self.set_controls("paused" if self.paused else "running")
        self.log("已暂停" if self.paused else "已继续", "warn")
        self._notify("已暂停，点击「继续」恢复" if self.paused else "已恢复监控",
                     WARN if self.paused else OK)

    def stop(self):
        if not self.running:
            return
        self.log("正在停止…", "warn")
        self.running = False
        self.paused = False
        self.set_controls("stopping")
        self._notify("已收到停止指令，正在收尾", WARN)

    def _loop(self):
        """主循环：单层截图 → 变化判定 → 交给状态机处理 → 重取基准。

        恒定存在的半透明浮层对「变化检测」无害（基准帧与当前帧都包含它），
        因此不再每轮隐藏/显示浮层，省掉两次跨线程 UI 同步与屏幕闪烁。
        浮层只在真正点击前才隐藏，避免点击打到自己的置顶窗口。
        """
        base = None
        t_start = time.monotonic()
        last_send = 0.0
        try:
            while self.running:
                if self.paused:
                    time.sleep(0.2)
                    continue
                poll = max(0.12, float(self.rt.get("poll_ms", 600)) / 1000.0)
                t0 = time.monotonic()
                try:
                    pts = int(self.rt.get("sample_points", 144))
                    tol = int(self.rt.get("change_tol", 18))
                    ratio_thr = float(self.rt.get("change_ratio", 0.04))
                    quiet = float(self.rt.get("startup_quiet", 2.5))
                    cur = snapshot(self.rect_of("monitor"), pts)
                    self.last_ms = (time.monotonic() - t0) * 1000

                    if base is None or time.monotonic() - t_start < quiet:
                        base = cur       # 启动静默期只更新基准，不转发
                        time.sleep(poll)
                        continue
                    changed, ratio = signal_change(base, cur, ratio_thr, tol)
                    if not changed:
                        base = cur       # 吸收光标闪烁等微小抖动
                        time.sleep(poll)
                        continue
                    base = cur
                    self.log("检测区变化 %.0f%%" % (ratio * 100))
                    last_send = self._handle_change(last_send)
                    # 一轮流程会改变屏幕（新消息滚动、AI 回复），结束后重取基准，
                    # 否则下一轮会把自己的操作误判成新消息。
                    base = snapshot(self.rect_of("monitor"), pts)
                    time.sleep(poll)
                except Exception:
                    self.last_err = "RuntimeError"
                    self.log("循环异常，已自动恢复", "err")
                    base = None
                    time.sleep(0.5)
        finally:
            self.running = False
            self.post(self._loop_end)
            self.clipguard.restore()

    def _handle_change(self, last_send):
        """一次完整转发事务。返回更新后的最近发送时间。"""
        mode = self.rt.get("run_mode", "both")
        cooldown = float(self.rt.get("cooldown", 1.5))
        if mode == "detect":
            self.msg_count += 1
            self.post(self._update_status)
            return last_send
        if time.monotonic() - last_send < cooldown:
            return last_send

        text = self.read_message().strip()
        if len(text) < 2:
            return last_send
        h = fp(text)
        self.log("新消息：%s" % self.describe(text))
        if h in self.seen:
            self.log("跳过：同一条消息已处理", "warn")
            return last_send
        self.seen[h] = time.monotonic()   # 先登记，失败也不重复骚扰
        if len(self.seen) > 80:
            self.seen.popitem(last=False)

        if not self.forward_to_ai(text):
            return last_send
        self.log("已转发到 AI")
        last_send = time.monotonic()
        if mode == "forward":
            self.msg_count += 1
            self.last_ok = datetime.now().strftime("%H:%M:%S")
            self.post(self._update_status)
            return last_send

        if not self.running or not self.wait_reply_ready():
            return last_send
        reply = self.read_reply().strip()
        if len(reply) < 2:
            self.log("回复为空，跳过回发", "warn")
            return last_send
        rh = fp(reply)
        if rh == self.last_reply_hash or rh == h:
            self.log("跳过：回复与上轮相同", "warn")
            return last_send
        if self.send_back(reply):
            self.last_reply_hash = rh
            self.msg_count += 1
            self.last_ok = datetime.now().strftime("%H:%M:%S")
            self.log("已回发到聊天窗口", "ok")
        self.post(self._update_status)
        return last_send

    def _loop_end(self):
        try:
            self.set_controls("idle")
            self.ghost(False)
            self.log("监控已停止", "ok")
            self._notify("监控已停止", OK)
        except Exception:
            pass

    # ---------------- 退出 ----------------
    def quit(self):
        self.running = False
        try:
            self.save()
            LOGGER.write("STOP %s" % VERSION)
        except Exception:
            pass
        try:
            if self.worker and self.worker.is_alive():
                self.worker.join(timeout=1.5)
        except Exception:
            pass
        for m in self.markers.values():
            m.destroy()
        for r in self.regions.values():
            r.destroy()
        self.clipguard.restore()
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.log("%s %s 已就绪 · 屏幕 %dx%d" % (APP_NAME, VERSION, SCREEN_W, SCREEN_H), "ok")
        self.log("作者 %s · %s" % (AUTHOR, REPO))
        self.log("提示：首次使用请看「快速上手」，先用单步测试校准。")
        self.check_components()
        self._update_status()
        self.root.mainloop()


def main():
    try:
        App().run()
    except Exception as e:
        # 崩溃信息也不写本地路径
        try:
            messagebox.showerror(APP_NAME, "启动失败：%s" % sanitize(type(e).__name__))
        except Exception:
            pass


if __name__ == "__main__":
    main()
