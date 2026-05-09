#!/usr/bin/env python3
"""
AutoWatcher 🔵 - 统一文件夹监控后台
监���桌面对应文件夹，出现素材自动处理：
  - 待修服装图/ → 美图云修 API
  - 待剪素材/  → 加水印 + 分割
  - 待上传素材/ → 百度网盘上传
"""

import os
import sys
import json
import time
import hashlib
import logging
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path
from threading import Thread, Event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.expanduser("~/autowatcher.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("autowatcher")

HOME = os.path.expanduser("~")
CONFIG_FILE = os.path.join(HOME, ".openclaw", "scripts", "autowatcher_config.json")
STATE_FILE = os.path.join(HOME, ".openclaw", "scripts", "autowatcher_state.json")

DEFAULT_CONFIG = {
    "monitor_interval": 5,
    "watermark": {
        "enabled": True,
        "image": os.path.join(HOME, "Desktop/AI橱窗图/AI换背景/水印.png"),
        "position": "bottom-right",
        "scale": 0.25,
        "opacity": 0.8,
    },
    "retouch": {
        "api_key": "",
        "api_secret": "",
        "media_code": "MTyunxiu1d4205d645",
    },
    "watchers": {
        "retouch": {
            "enabled": True,
            "watch_dir": os.path.join(HOME, "Desktop/待修服装图"),
            "done_dir": os.path.join(HOME, "Desktop/待修服装图/已修"),
        },
        "watermark": {
            "enabled": False,  # 已由 AutoCutStudio 接管
            "watch_dir": os.path.join(HOME, "Desktop/AI水印"),
        },
        "upload": {
            "enabled": False,  # bypy 授权过期，暂关闭
            "watch_dir": os.path.join(HOME, "待上传素材"),
        },
    },
}


class AutoWatcher:
    def __init__(self):
        self.config = self._load_config()
        self.state = self._load_state()
        self.stop_event = Event()
        self.running = False
        self.threads = []

    def _load_config(self):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
                # 合并默认值
                merged = DEFAULT_CONFIG.copy()
                merged.update(cfg)
                return merged
        except (FileNotFoundError, json.JSONDecodeError):
            return DEFAULT_CONFIG.copy()

    def _save_config(self):
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    def _load_state(self):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"processed_files": {}}

    def _save_state(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def _is_processed(self, filepath, watcher_name):
        """检查文件是否已处理（按路径 + 修改时间）"""
        stat = os.stat(filepath)
        key = f"{watcher_name}:{filepath}"
        cached = self.state["processed_files"].get(key)
        if cached == f"{os.path.getsize(filepath)}_{int(stat.st_mtime)}":
            return True
        return False

    def _mark_processed(self, filepath, watcher_name):
        key = f"{watcher_name}:{filepath}"
        try:
            self.state["processed_files"][key] = f"{os.path.getsize(filepath)}_{int(os.stat(filepath).st_mtime)}"
            # 限制状态大小，只保留最近500条
            if len(self.state["processed_files"]) > 500:
                old_keys = sorted(self.state["processed_files"].keys())[:-400]
                for k in old_keys:
                    del self.state["processed_files"][k]
            self._save_state()
        except Exception:
            pass

    def _get_new_files(self, watch_dir, watcher_name):
        """获取未处理的视频/图片文件"""
        video_exts = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".m4v"}
        image_exts = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp"}
        all_exts = video_exts | image_exts

        if not os.path.isdir(watch_dir):
            return []

        files = []
        for f in Path(watch_dir).iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in all_exts:
                continue
            if f.name.startswith("._"):
                continue
            # 忽略临时文件（可能被其他进程生成）
            if f.name.endswith(('.tmp.mp4', '.wm_tmp.mp4', '.watermarked.tmp.mp4')):
                continue
            if not self._is_processed(str(f), watcher_name):
                files.append(f)
        return sorted(files)

    # ═══════════════════════════════════════════
    #  🖼️  修图监控
    # ═══════════════════════════════════════════

    def _watch_retouch(self):
        cfg = self.config["watchers"]["retouch"]
        watch_dir = cfg["watch_dir"]
        done_dir = cfg["done_dir"]
        rc = self.config["retouch"]

        os.makedirs(watch_dir, exist_ok=True)
        os.makedirs(done_dir, exist_ok=True)

        while not self.stop_event.is_set():
            try:
                files = self._get_new_files(watch_dir, "retouch")
                for img_path in files:
                    if self.stop_event.is_set():
                        break
                    log.info(f"🖼️ [修图] 新图片: {img_path.name}")
                    if rc["api_key"] and rc["api_secret"]:
                        self._submit_retouch(str(img_path), rc, done_dir)
                    else:
                        log.info(f"  ⚠️ API Key 未设置，跳过修图")
                        # 没 API key 就直接标记为已处理
                        self._mark_processed(str(img_path), "retouch")
                    time.sleep(2)
            except Exception as e:
                log.error(f"  ❌ 修图监控异常: {e}")
            time.sleep(self.config["monitor_interval"])

    def _submit_retouch(self, img_path, rc, done_dir):
        """提交修图到美图云修"""
        filename = os.path.basename(img_path)
        log.info(f"  🔄 提交修图: {filename}")

        try:
            # 先尝试用 file URL — 实际需要公网可访问的图床
            # 这里先做本地路径，需要用户确保可用
            media_data = f"file://{img_path}"

            # 换成更可靠的方式：直接传图片内容（base64）
            # 但 API 要求 URL，所以我们先尝试 file://
            payload = json.dumps({
                "api_key": rc["api_key"],
                "api_secret": rc["api_secret"],
                "media_code": rc["media_code"],
                "media_data": media_data,
                "parameter": {},
                "repost_url": "",
            }).encode("utf-8")

            req = urllib.request.Request(
                "https://openapi-yunxiu.meitu.com/openapi/chain.json",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())

            if result.get("code") == 0:
                chain_id = result["data"]["chain_id"]
                log.info(f"  ✅ 提交成功, chain_id: {chain_id}")
                # 移到已修目录
                self._move_to_dir(img_path, done_dir)
            else:
                log.info(f"  ❌ 提交失败: {result}")
                # 失败也移动，避免重复提交失败
                self._move_to_dir(img_path, done_dir)

            self._mark_processed(img_path, "retouch")

        except Exception as e:
            log.error(f"  ❌ 修图异常: {e}")
            self._mark_processed(img_path, "retouch")

    # ═══════════════════════════════════════════
    #  💧  水印监控
    # ═══════════════════════════════════════════

    def _watch_watermark(self):
        cfg = self.config["watchers"]["watermark"]
        wm_cfg = self.config["watermark"]
        watch_dir = cfg["watch_dir"]

        os.makedirs(watch_dir, exist_ok=True)

        while not self.stop_event.is_set():
            try:
                files = self._get_new_files(watch_dir, "watermark")
                for video_path in files:
                    if self.stop_event.is_set():
                        break
                    log.info(f"💧 [水印] 新素材: {video_path.name}")

                    if wm_cfg["enabled"]:
                        # 原地替换：加水印后覆盖原文件
                        self._add_watermark_inplace(str(video_path), wm_cfg)
                    else:
                        self._mark_processed(str(video_path), "watermark")

                    self._mark_processed(str(video_path), "watermark")
                    time.sleep(1)

            except Exception as e:
                log.error(f"  ❌ 水印监控异常: {e}")
            time.sleep(self.config["monitor_interval"])

    def _add_watermark_inplace(self, video_path, wm_cfg):
        """给视频加水印后原地覆盖原文件"""
        filename = os.path.basename(video_path)
        wm_img = wm_cfg["image"]

        if not os.path.isfile(wm_img):
            log.warning(f"  ⚠️ 水印图片不存在: {wm_img}，跳过")
            return

        pos_map = {
            "top-left": "10:10",
            "top-right": "W-w-10:10",
            "bottom-left": "10:H-h-10",
            "bottom-right": "W-w-10:H-h-10",
            "center": "(W-w)/2:(H-h)/2",
        }
        pos = pos_map.get(wm_cfg["position"], "W-w-10:H-h-10")
        scale_ratio = wm_cfg["scale"]

        # 输出到临时文件，成功后替换原文件
        temp_output = video_path + ".watermarked.tmp.mp4"

        filter_complex = (
            f"[1:v]scale=iw*{scale_ratio}:-1[wm];"
            f"[0:v][wm]overlay={pos}:format=auto,format=yuv420p[v]"
        )

        cmd = [
            "ffmpeg", "-i", video_path, "-i", wm_img,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "0:a?",
            "-c:a", "copy",
            "-y", temp_output,
        ]

        try:
            log.info(f"  💧 添加水印: {filename} -> 原地替换")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0 and os.path.getsize(temp_output) > 0:
                # 替换原文件
                os.replace(temp_output, video_path)
                log.info(f"  ✅ 水印完成，原地替换: {filename}")
            else:
                log.warning(f"  ⚠️ 水印失败: {result.stderr[-200:]}")
                self._cleanup_temp(temp_output)
        except subprocess.TimeoutExpired:
            log.error(f"  ❌ 水印超时: {filename}")
            self._cleanup_temp(temp_output)
        except Exception as e:
            log.error(f"  ❌ 水印异常: {e}")
            self._cleanup_temp(temp_output)

    def _cleanup_temp(self, path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _move_to_dir(self, src, dst_dir):
        """移动文件到目标目录"""
        try:
            dst = os.path.join(dst_dir, os.path.basename(src))
            if os.path.exists(dst):
                name, ext = os.path.splitext(os.path.basename(src))
                dst = os.path.join(dst_dir, f"{name}_{int(time.time())}{ext}")
            os.rename(src, dst)
            log.info(f"  📦 已移动到: {os.path.basename(dst)}")
        except Exception as e:
            log.error(f"  ❌ 移动文件失败: {e}")

    # ═══════════════════════════════════════════
    #  📦  上传监控
    # ═══════════════════════════════════════════

    def _watch_upload(self):
        cfg = self.config["watchers"]["upload"]
        watch_dir = cfg["watch_dir"]
        os.makedirs(watch_dir, exist_ok=True)

        ctl_script = os.path.join(HOME, "openclaw/baidu_upload_ctl.sh")

        while not self.stop_event.is_set():
            try:
                # 检查是否有新文件（上传脚本自己会处理，这里只做保活）
                status = subprocess.run(
                    ["bash", ctl_script, "status"],
                    capture_output=True, text=True, timeout=5,
                )
                if status.stdout.strip() != "running":
                    log.info("📦 上传监控未运行，启动中...")
                    subprocess.run(["bash", ctl_script, "start"], timeout=10)
            except Exception as e:
                log.error(f"  ❌ 上传保活异常: {e}")
            time.sleep(30)

    # ═══════════════════════════════════════════
    #  启动 / 停止
    # ═══════════════════════════════════════════

    def _process_existing_files(self):
        """启动时扫描各监控目录中已有的文件，立即处理"""
        log.info("🔍 扫描已有文件...")
        for name, cfg in self.config["watchers"].items():
            if not cfg["enabled"]:
                continue
            watch_dir = cfg["watch_dir"]
            if not os.path.isdir(watch_dir):
                continue
            count = 0
            for f in Path(watch_dir).iterdir():
                if not f.is_file() or f.name.startswith("._"):
                    continue
                if not self._is_processed(str(f), name):
                    count += 1
            if count > 0:
                log.info(f"  📁 {name}: {count} 个文件待处理")

    def start(self):
        if self.running:
            log.warning("已在运行")
            return

        self.stop_event.clear()
        self.running = True
        log.info("=" * 50)
        log.info("🔵 AutoWatcher 后台服务启动")
        log.info("=" * 50)

        self._process_existing_files()

        watchers = []

        if self.config["watchers"]["retouch"]["enabled"]:
            t = Thread(target=self._watch_retouch, daemon=True)
            t.start()
            watchers.append("🖼️ 修图")
            self.threads.append(t)

        if self.config["watchers"]["watermark"]["enabled"]:
            t = Thread(target=self._watch_watermark, daemon=True)
            t.start()
            watchers.append("💧 水印")
            self.threads.append(t)

        if self.config["watchers"]["upload"]["enabled"]:
            t = Thread(target=self._watch_upload, daemon=True)
            t.start()
            watchers.append("📦 上传")
            self.threads.append(t)

        log.info(f"📡 监控中: {', '.join(watchers)}")

        # 打印各监控目录
        for name, cfg in self.config["watchers"].items():
            if cfg["enabled"]:
                log.info(f"  📁 {name}: {cfg['watch_dir']}")

        # 主线程保持
        try:
            while not self.stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        log.info("⏹ AutoWatcher 停止中...")
        self.stop_event.set()
        self.running = False


if __name__ == "__main__":
    watcher = AutoWatcher()
    watcher.start()
