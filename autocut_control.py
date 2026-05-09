#!/usr/bin/env python3
"""
AutoCutStudio 🔵 - 手势剪辑 + 水印处理 合并工具
👌 手势剪辑: 拖视频到待剪素材 → 自动检测👌手势 → 剪辑有效片段
💧 水印处理: 拖视频到AI水印 → 自动加水印 → 原地替换
支持监控模式：开关打开后自动处理新文件
"""

import os
import sys
import json
import time
import subprocess
import logging
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog
from pathlib import Path
from threading import Thread, Event

def _bundle_path():
    """
    返回打包资源根目录。
    支持三种运行模式：
    1. PyInstaller 打包后: sys._MEIPASS
    2. .app 内嵌脚本: ../Resources (相对于 MacOS/)
    3. 开发模式: 脚本所在目录
    """
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS)
    # .app 运行模式: 脚本在 Resources/ 下
    script_dir = Path(__file__).parent.resolve()
    if script_dir.name == "Resources" or script_dir.name == "scripts":
        return script_dir
    # 开发模式: 脚本同目录
    return script_dir

BUNDLE_DIR = _bundle_path()

# 自动找到 ffmpeg/ffprobe（先找包内，再找系统）
_BUNDLE_FFMPEG = BUNDLE_DIR / "ffmpeg" / "ffmpeg"
_BUNDLE_FFPROBE = BUNDLE_DIR / "ffmpeg" / "ffprobe"
FFMPEG = str(_BUNDLE_FFMPEG) if _BUNDLE_FFMPEG.is_file() else "ffmpeg"
FFPROBE = str(_BUNDLE_FFPROBE) if _BUNDLE_FFPROBE.is_file() else "ffprobe"

# 自动找到手势模型
_MODEL_CANDIDATES = [
    BUNDLE_DIR / "hand_landmarker.task",
    BUNDLE_DIR.parent / "hand_landmarker.task",  # 有时打包后上一层
    Path.home() / ".openclaw/models/hand_landmarker.task",
]
MP_MODEL = None
for c in _MODEL_CANDIDATES:
    if c.is_file():
        MP_MODEL = str(c)
        break
if not MP_MODEL:
    MP_MODEL = os.path.expanduser("~/.autocutstudio/models/hand_landmarker.task")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.expanduser("~/autocut.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("autocut")

HOME = os.path.expanduser("~")
CFG_FILE = os.path.expanduser("~/.openclaw/scripts/autocut_config.json")
STATE_FILE = os.path.expanduser("~/.openclaw/scripts/autocut_state.json")
DEFAULT_CFG = {
    "gesture_in": os.path.join(HOME, "Desktop/待剪素材"),
    "gesture_out": os.path.join(HOME, "Desktop/待剪素材/5秒素材"),
    "gesture_delay": 1,
    "gesture_duration": 5,
    "gesture_watermark": True,
    "gesture_watch": False,
    "wm_dir": os.path.join(HOME, "Desktop/AI水印"),
    "wm_image": os.path.join(HOME, "Desktop/AI橱窗图/AI换背景/水印.png"),
    "wm_position": "bottom-right",
    "wm_scale": 1.0,
    "wm_watch": False,
    "wm_offset_x": 10,
    "wm_offset_y": 10,
    "wm_preview_video": "",
}

# mediapipe
try:
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions
    from mediapipe.tasks.python.vision import HandLandmarker, HandLandmarkerOptions, RunningMode
    from mediapipe import Image as MpImage, ImageFormat
    import numpy as np
    MP_OK = True
except ImportError:
    MP_OK = False


def is_ok(lm):
    if not lm or len(lm) < 21:
        return False
    d = ((lm[4].x - lm[8].x)**2 + (lm[4].y - lm[8].y)**2)**0.5
    return (d < 0.10  # 拇指食指距离（放松阈值）
            and lm[12].y < lm[10].y  # 中指伸直
            and lm[16].y < lm[14].y  # 无名指伸直
            and lm[20].y < lm[18].y)  # 小指伸直


def detect_ok_end(video_path):
    """
    检测👌手势，返回手完全离开画面的时间(秒)。
    逻辑：检测到👌手势后，继续检测直到手完全消失。
    剪辑时从手消失+1秒开始，确保成片开头干净无手。
    """
    import cv2, numpy as np
    from mediapipe import Image as MpImage, ImageFormat

    if not os.path.isfile(MP_MODEL):
        log.error("❌ 模型文件不存在"); return None

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        log.error("❌ 无法打开视频"); return None
    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    log.info(f"  📐 {total}帧 {fps:.1f}fps")

    hl = HandLandmarker.create_from_options(HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=MP_MODEL),
        running_mode=RunningMode.IMAGE, num_hands=2,
        min_hand_detection_confidence=0.3))

    sample = max(1, int(fps / 6))  # ~0.17秒采样
    found_ok = False       # 是否曾检测到👌手势
    hand_gone_ts = -1.0    # 手完全离开画面的时间

    try:
        for fc in range(0, total, sample):
            cap.set(cv2.CAP_PROP_POS_FRAMES, fc)
            ret, frame = cap.read()
            if not ret: break
            ts = fc / fps
            h2, w2 = frame.shape[:2]
            small = cv2.resize(frame, (640, int(640 * h2 / w2)))
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            r = hl.detect(MpImage(image_format=ImageFormat.SRGB, data=rgb))
            
            has_hand = bool(r.hand_landmarks)
            ok_now = any(is_ok(lm) for lm in r.hand_landmarks) if has_hand else False

            if ok_now:
                if not found_ok:
                    log.info(f"    👌 手势开始 @ {ts:.1f}s")
                found_ok = True
            elif has_hand:
                log.debug(f"    手在画面中(非👌) @ {ts:.1f}s")
            else:  # 无手
                if found_ok and hand_gone_ts < 0:
                    hand_gone_ts = ts
                    log.info(f"    ✋ 手离开画面 @ {ts:.1f}s")
    finally:
        cap.release()
        hl.close()

    if not found_ok:
        log.info("  ⏭ 未检测到手势")
        return None

    if hand_gone_ts < 0:
        # 手一直没离开画面（可能录到结尾手还在）
        log.info(f"  ⚠️ 手一直在画面中，返回视频结尾")
        return total / fps

    log.info(f"  ✅ 手消失 @ {hand_gone_ts:.2f}s")
    return hand_gone_ts


def cut_video_clip(input_path, ok_end, delay, duration, output_dir):
    name = os.path.basename(input_path)
    base = os.path.splitext(name)[0]
    ext = os.path.splitext(name)[1]
    probe = subprocess.run([
        FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", input_path
    ], capture_output=True, text=True, timeout=15)
    total = float(probe.stdout.strip() or 9999)
    start = min(ok_end + delay, total)
    end = min(ok_end + delay + duration, total)
    clip_dur = end - start
    if clip_dur < 1:
        log.info(f"  ⏭ 片段过短: {clip_dur:.1f}s")
        return None
    clip_dir = os.path.join(output_dir, base)
    os.makedirs(clip_dir, exist_ok=True)
    clip_path = os.path.join(clip_dir, f"{base}_clip{ext}")
    log.info(f"  ✂️ {start:.1f}s ~ {end:.1f}s")
    subprocess.run([
        FFMPEG, "-ss", str(start), "-i", input_path, "-t", str(clip_dur),
        "-c:v", "libx264", "-preset", "fast", "-c:a", "aac",
        "-avoid_negative_ts", "1", "-y", clip_path
    ], capture_output=True, text=True, timeout=120)
    log.info(f"  ✅ → {clip_path}")
    return clip_path


def add_watermark(input_path, wm_image, position, scale, offset_x=10, offset_y=10):
    """
    给视频加静态水印。
    offset_x/offset_y: 从预设锚点的像素偏移（正=向内，负=向外）
    """
    temp_path = input_path + ".wm_tmp.mp4"
    pos_map = {
        "top-left":      f"{offset_x}:{offset_y}",
        "top-right":     f"W-w-{offset_x}:{offset_y}",
        "bottom-left":   f"{offset_x}:H-h-{offset_y}",
        "bottom-right":  f"W-w-{offset_x}:H-h-{offset_y}",
        "center":        f"(W-w)/2+({offset_x}-10):(H-h)/2+({offset_y}-10)",
    }
    pos = pos_map.get(position, f"W-w-{offset_x}:H-h-{offset_y}")
    flt = f"[1:v]scale=iw*{scale}:-1[wm];[0:v][wm]overlay={pos}:format=auto,format=yuv420p[v]"
    r = subprocess.run([
        FFMPEG, "-i", input_path, "-i", wm_image,
        "-filter_complex", flt, "-map", "[v]", "-map", "0:a?",
        "-c:a", "copy", "-y", temp_path
    ], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {r.stderr[-300:]}")
    os.replace(temp_path, input_path)
    return True


# ============================================================
#  GUI
# ============================================================

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("AutoCutStudio 🔵")
        self.root.geometry(f"800x650+{(root.winfo_screenwidth()-800)//2}+{(root.winfo_screenheight()-650)//2}")
        self.root.minsize(700, 550)

        self.cfg = self._load()
        self._state = self._load_state()
        self._stop_gesture = Event()
        self._stop_wm = Event()
        self._gesture_thread = None
        self._wm_thread = None

        self._build()
        self._log("🔵 AutoCutStudio 已启动")
        self._log(f"👌 手势剪辑: {self.cfg['gesture_in']}")
        self._log(f"💧 水印: {self.cfg['wm_dir']}")
        if not MP_OK:
            self._log("❌ mediapipe 未安装")

    def _build(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self._tab_gesture(nb)
        self._tab_watermark(nb)
        self._tab_config(nb)
        self._tab_log(nb)
        self.sb = ttk.Label(self.root, text="就绪", relief=tk.SUNKEN, anchor=tk.W)
        self.sb.pack(fill=tk.X, padx=10, pady=(0, 5))

    # ── 标签1: 手势剪辑 ──

    def _tab_gesture(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="👌 手势剪辑")

        ttk.Label(f, text="视频拖入待剪素材后自动剪辑有效片段", font=("", 11)).pack(anchor=tk.W)

        # 监控开关
        wf = ttk.Frame(f)
        wf.pack(fill=tk.X, pady=5)
        self.g_watch = tk.BooleanVar(value=self.cfg.get("gesture_watch", False))
        self.g_watch_btn = ttk.Checkbutton(wf, text="🔴 监控开关 (ON时自动处理新素材)",
                                            variable=self.g_watch, command=self._toggle_gesture_watch)
        self.g_watch_btn.pack(side=tk.LEFT)
        self.g_watch_label = ttk.Label(wf, text=" [监控中]" if self.g_watch.get() else " [已关闭]",
                                       foreground="green" if self.g_watch.get() else "gray")
        self.g_watch_label.pack(side=tk.LEFT, padx=5)

        b = ttk.Frame(f)
        b.pack(fill=tk.X, pady=5)
        ttk.Button(b, text="🔍 分析所有视频", command=self._run_gesture, width=16).pack(side=tk.LEFT, padx=3)
        ttk.Button(b, text="📂 素材", command=lambda: os.system(f"open '{self.cfg['gesture_in']}'"), width=8).pack(side=tk.LEFT, padx=3)
        ttk.Button(b, text="📂 输出", command=lambda: os.system(f"open '{self.cfg['gesture_out']}'"), width=8).pack(side=tk.LEFT, padx=3)

        p = ttk.LabelFrame(f, text="参数", padding=5)
        p.pack(fill=tk.X, pady=5)
        ttk.Label(p, text="手势消失后延迟(秒):").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.g_delay = tk.IntVar(value=self.cfg["gesture_delay"])
        ttk.Spinbox(p, from_=0, to=10, textvariable=self.g_delay, width=5).grid(row=0, column=1, sticky=tk.W, padx=5)
        ttk.Label(p, text="保留(秒):").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.g_dur = tk.IntVar(value=self.cfg["gesture_duration"])
        ttk.Spinbox(p, from_=1, to=60, textvariable=self.g_dur, width=5).grid(row=1, column=1, sticky=tk.W, padx=5)
        self.g_wm = tk.BooleanVar(value=self.cfg["gesture_watermark"])
        ttk.Checkbutton(p, text="剪辑后自动加水印", variable=self.g_wm).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=5)

        # 状态显示
        self.g_status = ttk.Label(f, text="就绪", foreground="green")
        self.g_status.pack(anchor=tk.W, pady=5)

        # 已处理文件列表（简略）
        sf = ttk.LabelFrame(f, text="已处理文件", padding=3)
        sf.pack(fill=tk.BOTH, expand=True, pady=5)
        self.g_list = tk.Text(sf, height=5, font=("Menlo", 9), fg="gray")
        self.g_list.pack(fill=tk.BOTH, expand=True)
        self._refresh_g_list()

    # ── 标签2: 水印 ──

    def _tab_watermark(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="💧 水印")

        ttk.Label(f, text="视频拖入AI水印后自动加水印并原地替换", font=("", 11)).pack(anchor=tk.W)

        wf = ttk.Frame(f)
        wf.pack(fill=tk.X, pady=5)
        self.w_watch = tk.BooleanVar(value=self.cfg.get("wm_watch", False))
        self.w_watch_btn = ttk.Checkbutton(wf, text="🔴 监控开关 (ON时自动处理新素材)",
                                            variable=self.w_watch, command=self._toggle_wm_watch)
        self.w_watch_btn.pack(side=tk.LEFT)
        self.w_watch_label = ttk.Label(wf, text=" [监控中]" if self.w_watch.get() else " [已关闭]",
                                       foreground="green" if self.w_watch.get() else "gray")
        self.w_watch_label.pack(side=tk.LEFT, padx=5)

        b = ttk.Frame(f)
        b.pack(fill=tk.X, pady=5)
        ttk.Button(b, text="💧 处理所有视频", command=self._run_watermark, width=16).pack(side=tk.LEFT, padx=3)
        ttk.Button(b, text="📂 AI水印", command=lambda: os.system(f"open '{self.cfg['wm_dir']}'"), width=8).pack(side=tk.LEFT, padx=3)

        # ── 水印参数区 ──
        p = ttk.LabelFrame(f, text="水印参数", padding=8)
        p.pack(fill=tk.X, pady=5)

        # 第0行: 水印图片
        ttk.Label(p, text="水印图片:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.wm_img = tk.StringVar(value=self.cfg["wm_image"])
        ttk.Entry(p, textvariable=self.wm_img, width=40).grid(row=0, column=1, columnspan=2, padx=5, sticky=tk.W)
        ttk.Button(p, text="浏览", command=self._pick_wm).grid(row=0, column=3, padx=2)

        # 第1行: 预设位置
        ttk.Label(p, text="位置:").grid(row=1, column=0, sticky=tk.W, pady=2)
        wf2 = ttk.Frame(p)
        wf2.grid(row=1, column=1, columnspan=3, sticky=tk.W)
        self.wm_pos = tk.StringVar(value=self.cfg["wm_position"])
        for t, v in [("🔼左上","top-left"), ("🔷居中","center"), ("🔺右下","bottom-right"), ("⬇️左下","bottom-left")]:
            rb = ttk.Radiobutton(wf2, text=t, variable=self.wm_pos, value=v, command=self._on_wm_param_change)
            rb.pack(side=tk.LEFT, padx=2)

        # 第2行: X/Y 偏移微调
        of = ttk.Frame(p)
        of.grid(row=2, column=0, columnspan=4, sticky=tk.W, pady=3)
        ttk.Label(of, text="X偏移:").pack(side=tk.LEFT)
        self.wm_ox = tk.IntVar(value=self.cfg.get("wm_offset_x", 10))
        sx = ttk.Spinbox(of, from_=0, to=500, textvariable=self.wm_ox, width=5, command=self._on_wm_param_change)
        sx.pack(side=tk.LEFT, padx=3)
        sx.bind("<KeyRelease>", lambda e: self._on_wm_param_change())
        ttk.Label(of, text="px").pack(side=tk.LEFT)
        ttk.Label(of, text="   Y偏移:").pack(side=tk.LEFT, padx=(10,0))
        self.wm_oy = tk.IntVar(value=self.cfg.get("wm_offset_y", 10))
        sy = ttk.Spinbox(of, from_=0, to=500, textvariable=self.wm_oy, width=5, command=self._on_wm_param_change)
        sy.pack(side=tk.LEFT, padx=3)
        sy.bind("<KeyRelease>", lambda e: self._on_wm_param_change())
        ttk.Label(of, text="px").pack(side=tk.LEFT)
        hint = ttk.Label(of, text="   (正值向画面内偏移)")
        hint.pack(side=tk.LEFT, padx=5)

        # 第3行: 大小
        ttk.Label(p, text="大小:").grid(row=3, column=0, sticky=tk.W, pady=2)
        sf2 = ttk.Frame(p)
        sf2.grid(row=3, column=1, columnspan=3, sticky=tk.W)
        self.wm_sc = tk.DoubleVar(value=self.cfg["wm_scale"])
        s = ttk.Scale(sf2, from_=0.05, to=2.0, variable=self.wm_sc, orient=tk.HORIZONTAL, length=200,
                       command=self._on_wm_scale_change)
        s.pack(side=tk.LEFT)
        self.wm_slbl = ttk.Label(sf2, text=f"{self.wm_sc.get():.0%}", width=5)
        self.wm_slbl.pack(side=tk.LEFT, padx=5)

        if not hasattr(self, 'wm_info_lbl'):
            self.wm_info_lbl = ttk.Label(sf2, text="", foreground="gray")
            self.wm_info_lbl.pack(side=tk.LEFT, padx=10)
        self._update_wm_info()

        # ── 预览区 ──
        self._build_preview(f)

        self.w_status = ttk.Label(f, text="就绪", foreground="green")
        self.w_status.pack(anchor=tk.W, pady=3)

        sf = ttk.LabelFrame(f, text="已处理文件", padding=3)
        sf.pack(fill=tk.BOTH, expand=True, pady=5)
        self.w_list = tk.Text(sf, height=4, font=("Menlo", 9), fg="gray")
        self.w_list.pack(fill=tk.BOTH, expand=True)
        self._refresh_w_list()

    # ═══════════════════════════════════
    #  水印预览 — 可拖拽
    # ═══════════════════════════════════
    # 全局状态：拖拽中
    _pv_dragging = False
    _pv_drag_start_x = 0
    _pv_drag_start_y = 0
    _pv_drag_off_x = 0
    _pv_drag_off_y = 0

    def _build_preview(self, parent):
        """构建可拖拽水印预览区域"""
        pf = ttk.LabelFrame(parent, text="📐 预览 (拖拽水印调整位置)", padding=5)
        pf.pack(fill=tk.X, pady=3)

        # 顶部：视频选择 + 快捷预设按钮
        slf = ttk.Frame(pf)
        slf.pack(fill=tk.X)
        self.pv_video = tk.StringVar(value=self.cfg.get("wm_preview_video", ""))
        ttk.Button(slf, text="📂 选视频做参考", command=self._pick_preview_video).pack(side=tk.LEFT, padx=2)
        self.pv_label = ttk.Label(slf, text="未选择", foreground="gray")
        self.pv_label.pack(side=tk.LEFT, padx=5)
        self.pv_res_label = ttk.Label(slf, text="", foreground="gray")
        self.pv_res_label.pack(side=tk.LEFT, padx=5)
        # 快捷预设按钮
        for t, v in [("左上","top-left"), ("右下","bottom-right")]:
            ttk.Button(slf, text=t, width=4,
                       command=lambda p=v: self._quick_set_pos(p)).pack(side=tk.RIGHT, padx=2)

        # Canvas 预览区（自动适配窗口宽度）
        cvf = ttk.Frame(pf)
        cvf.pack(fill=tk.X, pady=3)
        self.pv_canvas = tk.Canvas(cvf, bg="#2b2b2b", highlightthickness=1, highlightbackground="#555")
        self.pv_canvas.pack(fill=tk.X)

        # 绑定鼠标事件（拖拽水印）
        self.pv_canvas.bind("<Button-1>", self._pv_mouse_down)
        self.pv_canvas.bind("<B1-Motion>", self._pv_mouse_drag)
        self.pv_canvas.bind("<ButtonRelease-1>", self._pv_mouse_up)

        # Canvas 下方信息栏（两行）
        self.pv_coord_label = ttk.Label(cvf, text="拖拽水印可调整位置 | 点击预设快速定位",
                                        foreground="#aaa", font=("Menlo", 8))
        self.pv_coord_label.pack(anchor=tk.W, pady=(2, 0))
        self.pv_detail_label = ttk.Label(cvf, text="", foreground="#888", font=("Menlo", 8))
        self.pv_detail_label.pack(anchor=tk.W)

        # 存储状态
        self._pv_scale = 1.0
        self._pv_vw = 0
        self._pv_vh = 0
        self._pv_ox = 0  # canvas上视频区域偏移
        self._pv_oy = 0
        self._pv_wm_tk = None  # 保持 PhotoImage 引用

        # 初始渲染
        saved = self.cfg.get("wm_preview_video", "")
        if saved and os.path.isfile(saved):
            self.pv_video.set(saved)
            self.root.after(600, lambda: self._pv_resolve(saved))
        else:
            self.root.after(500, self._schedule_render)

    def _quick_set_pos(self, pos):
        """快速预设位置并重置偏移"""
        self.wm_pos.set(pos)
        self.wm_ox.set(10)
        self.wm_oy.set(10)
        self._on_wm_param_change()

    def _pick_preview_video(self):
        d = filedialog.askopenfilename(title="选择参考视频",
            initialdir=self.cfg.get("wm_dir", os.path.join(HOME, "Desktop/AI水印")),
            filetypes=[("视频", "*.mp4 *.mov *.avi *.mkv *.m4v"), ("所有", "*.*")])
        if not d:
            return
        self.pv_video.set(d)
        self.cfg["wm_preview_video"] = d
        self._save()
        self._pv_resolve(d)

    def _pv_resolve(self, video_path):
        """获取视频分辨率并更新预览"""
        try:
            r = subprocess.run([
                FFPROBE, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "default=noprint_wrappers=1", video_path
            ], capture_output=True, text=True, timeout=10)
            w = h = None
            for line in r.stdout.strip().split("\n"):
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k == "width":
                        w = int(v)
                    elif k == "height":
                        h = int(v)
            if w and h:
                self._pv_vw = w
                self._pv_vh = h
                short = os.path.basename(video_path)
                if len(short) > 25:
                    short = short[:22] + "..."
                self.pv_label.config(text=short)
                self.pv_res_label.config(text=f"{w}×{h}")
                self._schedule_render()
        except Exception as e:
            self._log(f"⚠️ 获取视频尺寸失败: {e}")

    def _schedule_render(self):
        """安全地调度下一次渲染"""
        try:
            if hasattr(self, "pv_canvas") and self.pv_canvas.winfo_exists():
                self.pv_canvas.update_idletasks()
                self.root.after_idle(self._render_preview)
        except Exception:
            pass

    def _on_wm_scale_change(self, val):
        self.wm_slbl.config(text=f"{float(val):.0%}")
        self._on_wm_param_change()

    def _on_wm_param_change(self, *_args):
        """参数变更或拖拽结束 -> 更新预览 + 保存"""
        self._update_wm_info()
        self._schedule_render()

    def _update_wm_info(self):
        """更新水印图片信息（左下角显示）"""
        if not hasattr(self, "wm_info_lbl"):
            return
        wm_path = self.wm_img.get()
        if os.path.isfile(wm_path):
            try:
                from PIL import Image
                img = Image.open(wm_path)
                self.wm_info_lbl.config(text=f"水印: {img.size[0]}×{img.size[1]}px", foreground="gray")
            except:
                self.wm_info_lbl.config(text="", foreground="gray")
        else:
            self.wm_info_lbl.config(text="⚠️ 图片不存在", foreground="red")

    # ── 鼠标拖拽 ──

    def _pv_mouse_down(self, evt):
        """鼠标按下：检查是否点在水印上"""
        cv = self.pv_canvas
        # 找最近的 tag 为 "wm_rect" 的矩形
        items = cv.find_withtag("wm_rect")
        if not items:
            return
        # 检查点击是否在某水印矩形范围内
        for item_id in items:
            coords = cv.coords(item_id)
            if len(coords) >= 4:
                x1, y1, x2, y2 = coords[:4]
                # 扩大点击区域 8px
                margin = 8
                if (x1 - margin <= evt.x <= x2 + margin and
                    y1 - margin <= evt.y <= y2 + margin):
                    self._pv_dragging = True
                    self._pv_drag_start_x = evt.x_root
                    self._pv_drag_start_y = evt.y_root
                    self._pv_drag_off_x = self.wm_ox.get()
                    self._pv_drag_off_y = self.wm_oy.get()
                    # 视觉反馈
                    cv.itemconfig("wm_rect", outline="#ffcc00", width=3)
                    break

    def _pv_mouse_drag(self, evt):
        """鼠标拖拽：实时计算新的偏移值"""
        if not self._pv_dragging or not self._pv_vw or not self._pv_vh:
            return
        # 计算鼠标在视频分辨率空间中的移动量
        scale = self._pv_scale
        if scale <= 0:
            return
        dx_px = int((evt.x_root - self._pv_drag_start_x) / scale + 0.5)
        dy_px = int((evt.y_root - self._pv_drag_start_y) / scale + 0.5)

        position = self.wm_pos.get()
        real_wmw = self._calc_wm_width()
        real_wmh = self._calc_wm_height()

        new_ox = self._pv_drag_off_x
        new_oy = self._pv_drag_off_y

        if position == "top-left":
            new_ox = max(0, self._pv_drag_off_x - dx_px)
            new_oy = max(0, self._pv_drag_off_y - dy_px)
        elif position == "top-right":
            new_ox = max(0, self._pv_drag_off_x + dx_px)
            new_oy = max(0, self._pv_drag_off_y - dy_px)
        elif position == "bottom-left":
            new_ox = max(0, self._pv_drag_off_x - dx_px)
            new_oy = max(0, self._pv_drag_off_y + dy_px)
        elif position == "bottom-right":
            new_ox = max(0, self._pv_drag_off_x + dx_px)
            new_oy = max(0, self._pv_drag_off_y + dy_px)
        elif position == "center":
            new_ox = max(0, self._pv_drag_off_x + dx_px)
            new_oy = max(0, self._pv_drag_off_y + dy_px)

        self.wm_ox.set(new_ox)
        self.wm_oy.set(new_oy)
        self._schedule_render()

    def _pv_mouse_up(self, evt):
        """鼠标释放：结束拖拽，恢复颜色，保存"""
        if self._pv_dragging:
            self._pv_dragging = False
            cv = self.pv_canvas
            try:
                cv.itemconfig("wm_rect", outline="#00ff88", width=2)
            except Exception:
                pass
            self._save()

    # ── 尺寸计算工具 ──

    def _calc_wm_width(self):
        """计算水印在视频分辨率中的像素宽度"""
        wm_path = self.wm_img.get()
        if not os.path.isfile(wm_path):
            return 100
        try:
            from PIL import Image
            wmw, _ = Image.open(wm_path).size
            return int(wmw * self.wm_sc.get())
        except:
            return 100

    def _calc_wm_height(self):
        """计算水印在视频分辨率中的像素高度"""
        wm_path = self.wm_img.get()
        if not os.path.isfile(wm_path):
            return 100
        try:
            from PIL import Image
            _, wmh = Image.open(wm_path).size
            return int(wmh * self.wm_sc.get())
        except:
            return 100

    def _render_preview(self):
        """在 Canvas 上渲染可拖拽的水印预览"""
        cv = self.pv_canvas
        cw = cv.winfo_width()
        if cw < 50:
            cw = 360
        # Canvas 高度根据窗口自动调整（竖屏深些，横屏浅些）
        ch = max(200, int(cw * 0.56))
        cv.config(height=ch)

        wm_path = self.wm_img.get()
        if not os.path.isfile(wm_path):
            cv.delete("all")
            cv.create_text(cw//2, ch//2, text="⚠️ 请先选择水印图片", fill="#888", font=("", 11))
            return

        # 确定视频分辨率
        if self._pv_vw and self._pv_vh:
            vw, vh = self._pv_vw, self._pv_vh
        else:
            vw, vh = 1080, 1920  # 默认竖屏（常见电商视频）

        # 计算缩放：让视频完整显示在 Canvas 内
        scale = min(cw / vw, ch / vh)
        self._pv_scale = scale
        dvw = int(vw * scale)
        dvh = int(vh * scale)
        self._pv_ox = (cw - dvw) // 2
        self._pv_oy = (ch - dvh) // 2
        ox = self._pv_ox
        oy = self._pv_oy

        # 清空
        cv.delete("all")

        # 视频区域背景
        cv.create_rectangle(ox, oy, ox+dvw, oy+dvh, fill="#1a1a1a", outline="#555", width=1)
        # 安全区域参考框（90%线）
        margin_x = int(dvw * 0.05)
        margin_y = int(dvh * 0.05)
        cv.create_rectangle(ox+margin_x, oy+margin_y, ox+dvw-margin_x, oy+dvh-margin_y,
                            outline="#444", dash=(3, 4), width=1)
        # 中线
        cv.create_line(ox + dvw//2, oy, ox + dvw//2, oy+dvh, fill="#333", width=1, dash=(2, 4))
        cv.create_line(ox, oy + dvh//2, ox+dvw, oy + dvh//2, fill="#333", width=1, dash=(2, 4))
        # 分辨率和视频信息
        info = f"{vw}×{vh}"
        if self._pv_vw and self._pv_vh:
            cv.create_text(ox + 5, oy + 5, text=info, fill="#555", font=("Menlo", 7), anchor=tk.NW)

        # 加载水印
        try:
            from PIL import Image as PILImage, ImageTk
            wm_img = PILImage.open(wm_path)
            wmw, wmh = wm_img.size
        except Exception:
            cv.create_text(cw//2, ch//2, text="⚠️ 水印加载失败", fill="#888", font=("", 10))
            return

        # 水印缩放
        wm_scale = self.wm_sc.get()
        real_wmw = int(wmw * wm_scale)
        real_wmh = int(wmh * wm_scale)

        position = self.wm_pos.get()
        off_x = self.wm_ox.get()
        off_y = self.wm_oy.get()

        # 计算在视频分辨率中的位置
        anchor_x = {
            "top-left": off_x,
            "top-right": vw - real_wmw - off_x,
            "bottom-left": off_x,
            "bottom-right": vw - real_wmw - off_x,
            "center": (vw - real_wmw) // 2 + (off_x - 10),
        }.get(position, vw - real_wmw - off_x)

        anchor_y = {
            "top-left": off_y,
            "top-right": off_y,
            "bottom-left": vh - real_wmh - off_y,
            "bottom-right": vh - real_wmh - off_y,
            "center": (vh - real_wmh) // 2 + (off_y - 10),
        }.get(position, vh - real_wmh - off_y)

        # 缩放到 Canvas
        cvx = ox + int(anchor_x * scale)
        cvy = oy + int(anchor_y * scale)
        cvw = max(4, int(real_wmw * scale))
        cvh = max(4, int(real_wmh * scale))

        # 渲染水印图片本身（缩放到预览尺寸）
        try:
            # 缩放水印到预览尺寸
            wm_resized = wm_img.resize((cvw, cvh), PILImage.LANCZOS)
            # 如果有 alpha 通道，创建兼容 PhotoImage 的 RGBA 预览
            if wm_resized.mode == "RGBA":
                # 合成到深色背景上显示
                bg = PILImage.new("RGBA", wm_resized.size, (26, 26, 26, 255))
                wm_composite = PILImage.alpha_composite(bg, wm_resized)
                wm_tk = ImageTk.PhotoImage(wm_composite.convert("RGB"))
            else:
                wm_tk = ImageTk.PhotoImage(wm_resized)
            # 保持引用防止被 GC
            self._pv_wm_tk = wm_tk
            cv.create_image(cvx, cvy, image=wm_tk, anchor=tk.NW, tags="wm_rect")
        except Exception:
            # 回退：纯色方框
            cv.create_rectangle(cvx, cvy, cvx+cvw, cvy+cvh,
                                fill="#00ff88", stipple="gray25", outline="#00ff88", width=1, tags="wm_rect")

        # 外框（拖拽用）
        cv.create_rectangle(cvx, cvy, cvx+cvw, cvy+cvh,
                            fill="", outline="#00ff88", width=2, tags="wm_rect")
        # 尺寸标签角标
        label_bg = cv.create_rectangle(cvx+cvw-65, cvy+cvh-14, cvx+cvw-2, cvy+cvh-2,
                                        fill="#222222", outline="", stipple="", tags="wm_rect")
        cv.create_text(cvx+cvw-33, cvy+cvh-8, text=f"{real_wmw}×{real_wmh}",
                       fill="#00ff88", font=("Menlo", 7, "bold"), tags="wm_rect")
        # 拖拽把手
        hs = min(10, cvw // 4, cvh // 4)
        if hs >= 6:
            hx = cvx + 2
            hy = cvy + 2
            cv.create_rectangle(hx, hy, hx+hs, hy+hs,
                                fill="white", outline="#00ff88", width=1, tags="wm_rect")

        # 信息栏更新
        pos_map_cn = {
            "top-left": "左上↖", "top-right": "右上↗",
            "bottom-left": "左下↙", "bottom-right": "右下↘", "center": "居中"
        }
        pos_name = pos_map_cn.get(position, position)

        try:
            self.pv_coord_label.config(
                text=f"{pos_name} | 水印左上角: ({anchor_x},{anchor_y}) | 尺寸: {real_wmw}×{real_wmh}px")
            self.pv_detail_label.config(
                text=f"X偏移: {off_x}px | Y偏移: {off_y}px | 缩放: {wm_scale:.0%}")
        except Exception:
            pass

        # 无参考视频提示
        if not self._pv_vw:
            cv.create_text(ox+dvw//2, oy+dvh-12,
                          text="(未选参考视频，默认竖屏)",
                          fill="#555", font=("", 8), anchor=tk.S)

    # ── 标签3: 配置 ──

    def _tab_config(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="⚙️ 配置")
        g = ttk.LabelFrame(f, text="目录", padding=8)
        g.pack(fill=tk.X, pady=5)
        ttk.Label(g, text="手势素材目录:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.c_gin = tk.StringVar(value=self.cfg["gesture_in"])
        ttk.Entry(g, textvariable=self.c_gin, width=45).grid(row=0, column=1, padx=5)
        ttk.Button(g, text="浏览", command=lambda: self._pick_dir(self.c_gin)).grid(row=0, column=2)
        ttk.Label(g, text="手势输出目录:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.c_gout = tk.StringVar(value=self.cfg["gesture_out"])
        ttk.Entry(g, textvariable=self.c_gout, width=45).grid(row=1, column=1, padx=5)
        ttk.Button(g, text="浏览", command=lambda: self._pick_dir(self.c_gout)).grid(row=1, column=2)
        ttk.Label(g, text="水印目录:").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.c_wm_dir = tk.StringVar(value=self.cfg["wm_dir"])
        ttk.Entry(g, textvariable=self.c_wm_dir, width=45).grid(row=2, column=1, padx=5)
        ttk.Button(g, text="浏览", command=lambda: self._pick_dir(self.c_wm_dir)).grid(row=2, column=2)
        ttk.Button(f, text="💾 保存", command=self._save).pack(pady=10)

    # ── 标签4: 日志 ──

    def _tab_log(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="📋 日志")
        self.log = scrolledtext.ScrolledText(f, wrap=tk.WORD, font=("Menlo", 10))
        self.log.pack(fill=tk.BOTH, expand=True)
        ttk.Button(f, text="清空", command=lambda: self.log.delete(1.0, tk.END)).pack(pady=3)

    # ═══════════════════════════════════
    #  监控线程
    # ═══════════════════════════════════

    def _toggle_gesture_watch(self):
        if self.g_watch.get():
            self._stop_gesture.clear()
            self._gesture_thread = Thread(target=self._gesture_watch_loop, daemon=True)
            self._gesture_thread.start()
            self.g_watch_label.config(text=" [监控中]", foreground="green")
            self.g_status.config(text="监控中", foreground="green")
            self._log("👌 手势监控已开启")
        else:
            self._stop_gesture.set()
            self.g_watch_label.config(text=" [已关闭]", foreground="gray")
            self.g_status.config(text="已暂停", foreground="orange")
            self._log("👌 手势监控已关闭")
        self._save()

    def _toggle_wm_watch(self):
        if self.w_watch.get():
            self._stop_wm.clear()
            self._wm_thread = Thread(target=self._wm_watch_loop, daemon=True)
            self._wm_thread.start()
            self.w_watch_label.config(text=" [监控中]", foreground="green")
            self.w_status.config(text="监控中", foreground="green")
            self._log("💧 水印监控已开启")
        else:
            self._stop_wm.set()
            self.w_watch_label.config(text=" [已关闭]", foreground="gray")
            self.w_status.config(text="已暂停", foreground="orange")
            self._log("💧 水印监控已关闭")
        self._save()

    def _gesture_watch_loop(self):
        video_exts = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".mts"}
        while not self._stop_gesture.is_set():
            indir = self.cfg["gesture_in"]
            os.makedirs(indir, exist_ok=True)
            found = False
            for f in sorted(Path(indir).iterdir()):
                if f.is_file() and f.suffix.lower() in video_exts and not f.name.startswith("._"):
                    fname = f.name
                    fkey = fname
                    if fkey not in self._state.get("gesture_done", set()):
                        self._sb(f"手势: {fname}")
                        self._log(f"\n📹 新素材: {fname}")
                        ok_end = detect_ok_end(str(f))
                        if ok_end is not None:
                            result = cut_video_clip(str(f), ok_end,
                                                    self.g_delay.get(), self.g_dur.get(),
                                                    self.cfg["gesture_out"])
                            if result and self.g_wm.get():
                                self._log("  💧 加剪辑水印...")
                                try:
                                    add_watermark(result, self.cfg["wm_image"],
                                                  self.cfg["wm_position"], self.cfg["wm_scale"],
                                                  self.cfg["wm_offset_x"], self.cfg["wm_offset_y"])
                                except Exception as e:
                                    self._log(f"  ⚠️ {e}")
                        self._state.setdefault("gesture_done", set()).add(fkey)
                        self._save_state()
                        self._refresh_g_list()
                        found = True
            if not found:
                self._stop_gesture.wait(5)

    def _wm_watch_loop(self):
        video_exts = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}
        while not self._stop_wm.is_set():
            wm_dir = self.cfg["wm_dir"]
            wm_img = self.cfg["wm_image"]
            os.makedirs(wm_dir, exist_ok=True)
            found = False
            for f in sorted(Path(wm_dir).iterdir()):
                if f.is_file() and f.suffix.lower() in video_exts and not f.name.startswith("._"):
                    fname = f.name
                    fkey = fname
                    if fkey not in self._state.get("wm_done", set()):
                        self._sb(f"水印: {fname}")
                        self._log(f"  💧 新素材: {fname}")
                        try:
                            add_watermark(str(f), wm_img, self.cfg["wm_position"], self.cfg["wm_scale"],
                                             self.cfg["wm_offset_x"], self.cfg["wm_offset_y"])
                            self._log(f"  ✅ {fname} 水印完成")
                        except Exception as e:
                            self._log(f"  ❌ {fname}: {e}")
                        self._state.setdefault("wm_done", set()).add(fkey)
                        self._save_state()
                        self._refresh_w_list()
                        found = True
            if not found:
                self._stop_wm.wait(5)

    # ═══════════════════════════════════
    #  手动功能
    # ═══════════════════════════════════

    def _run_gesture(self):
        self._save()
        self._log("🔍 开始手势分析...")
        Thread(target=self._do_gesture, daemon=True).start()

    def _do_gesture(self):
        if not MP_OK:
            self._log("❌ mediapipe 未安装"); return
        indir = self.cfg["gesture_in"]
        outdir = self.cfg["gesture_out"]
        os.makedirs(indir, exist_ok=True); os.makedirs(outdir, exist_ok=True)
        videos = sorted([f for f in Path(indir).iterdir()
                         if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".avi", ".mkv")])
        if not videos:
            self._log("📭 没有视频"); return
        self._log(f"📹 {len(videos)} 个视频")
        for v in videos:
            if not os.path.isfile(str(v)): continue
            self._sb(f"分析: {v.name}")
            self._log(f"\n🎬 {v.name}")
            ok_end = detect_ok_end(str(v))
            if ok_end is None: continue
            result = cut_video_clip(str(v), ok_end, self.g_delay.get(), self.g_dur.get(), outdir)
            if result and self.g_wm.get():
                self._log("  💧 加剪辑水印...")
                try:
                    add_watermark(result, self.cfg["wm_image"], self.cfg["wm_position"], self.cfg["wm_scale"],
                                     self.cfg["wm_offset_x"], self.cfg["wm_offset_y"])
                except Exception as e:
                    self._log(f"  ⚠️ {e}")
            self._log(f"✅ {v.name} 完成")
            # 标记为已处理
            fkey = v.name
            self._state.setdefault("gesture_done", set()).add(fkey)
        self._save_state()
        self._refresh_g_list()
        self._sb("分析完成")
        self._notify("🎬 手势分析完成")

    def _run_watermark(self):
        self._save()
        self._log("💧 开始处理水印...")
        Thread(target=self._do_watermark, daemon=True).start()

    def _do_watermark(self):
        wm_dir = self.cfg["wm_dir"]
        wm_img = self.cfg["wm_image"]
        os.makedirs(wm_dir, exist_ok=True)
        if not os.path.isfile(wm_img):
            self._log(f"❌ 水印图片不存在: {wm_img}"); return
        videos = sorted([f for f in Path(wm_dir).iterdir()
                         if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".avi", ".mkv")
                         and not f.name.startswith("._")
                         and not f.name.endswith(('.tmp.mp4', '.wm_tmp.mp4', '.watermarked.tmp.mp4'))])
        if not videos:
            self._log("📭 没有视频"); return
        count = 0
        for v in videos:
            self._sb(f"水印: {v.name}")
            self._log(f"  💧 {v.name}", end="")
            try:
                add_watermark(str(v), wm_img, self.cfg["wm_position"], self.cfg["wm_scale"],
                                 self.cfg["wm_offset_x"], self.cfg["wm_offset_y"])
                self._log(" ✅")
                count += 1
            except Exception as e:
                self._log(f" ❌ {e}")
            fkey = v.name
            self._state.setdefault("wm_done", set()).add(fkey)
        self._save_state()
        self._refresh_w_list()
        msg = f"✅ 水印完成: {count}/{len(videos)}"
        self._log(msg)
        self._sb(msg)
        self._notify(msg)

    # ═══════════════════════════════════
    #  工具
    # ═══════════════════════════════════

    def _pick_dir(self, var):
        d = filedialog.askdirectory(title="选择目录", initialdir=var.get() or HOME)
        if d:
            var.set(d)

    def _pick_wm(self):
        d = filedialog.askopenfilename(title="选择水印图片",
            initialdir=os.path.dirname(self.cfg["wm_image"]),
            filetypes=[("图片", "*.png *.jpg *.jpeg"), ("所有", "*.*")])
        if d:
            self.wm_img.set(d)
            self._on_wm_param_change()

    def _save(self):
        self.cfg["gesture_in"] = self.c_gin.get()
        self.cfg["gesture_out"] = self.c_gout.get()
        self.cfg["wm_dir"] = self.c_wm_dir.get()
        self.cfg["wm_image"] = self.wm_img.get()
        self.cfg["wm_position"] = self.wm_pos.get()
        self.cfg["wm_scale"] = self.wm_sc.get()
        self.cfg["gesture_delay"] = self.g_delay.get()
        self.cfg["gesture_duration"] = self.g_dur.get()
        self.cfg["gesture_watermark"] = self.g_wm.get()
        self.cfg["gesture_watch"] = self.g_watch.get()
        self.cfg["wm_watch"] = self.w_watch.get()
        self.cfg["wm_offset_x"] = self.wm_ox.get()
        self.cfg["wm_offset_y"] = self.wm_oy.get()
        self.cfg["wm_preview_video"] = self.pv_video.get()
        os.makedirs(os.path.dirname(CFG_FILE), exist_ok=True)
        with open(CFG_FILE, "w") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)

    def _load(self):
        try:
            with open(CFG_FILE) as f:
                return {**DEFAULT_CFG, **json.load(f)}
        except (FileNotFoundError, json.JSONDecodeError):
            return dict(DEFAULT_CFG)

    def _load_state(self):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
                s["gesture_done"] = set(s.get("gesture_done", []))
                s["wm_done"] = set(s.get("wm_done", []))
                return s
        except (FileNotFoundError, json.JSONDecodeError):
            return {"gesture_done": set(), "wm_done": set()}

    def _save_state(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({
                "gesture_done": list(self._state.get("gesture_done", set())),
                "wm_done": list(self._state.get("wm_done", set())),
            }, f, ensure_ascii=False, indent=2)

    def _refresh_g_list(self):
        if not hasattr(self, "g_list"):
            return
        self.g_list.delete(1.0, tk.END)
        items = list(self._state.get("gesture_done", set()))[-20:]
        for i in items:
            self.g_list.insert(tk.END, f"  ✅ {i.split('_', 1)[0]}\n")
        if not items:
            self.g_list.insert(tk.END, "  暂无\n")

    def _refresh_w_list(self):
        if not hasattr(self, "w_list"):
            return
        self.w_list.delete(1.0, tk.END)
        items = list(self._state.get("wm_done", set()))[-20:]
        for i in items:
            self.w_list.insert(tk.END, f"  ✅ {i.split('_', 1)[0]}\n")
        if not items:
            self.w_list.insert(tk.END, "  暂无\n")

    def _log(self, msg, end="\n"):
        log.info(msg)
        if hasattr(self, "log") and self.log.winfo_exists():
            self.log.insert(tk.END, msg + end)
            self.log.see(tk.END)
            self.root.update_idletasks()

    def _sb(self, text):
        self.sb.config(text=text)
        self.root.update_idletasks()

    def _notify(self, msg):
        try:
            subprocess.run(["osascript", "-e",
                f'display notification "{msg}" with title "AutoCutStudio" sound name "Glass"'
            ], timeout=3)
        except Exception:
            pass


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
