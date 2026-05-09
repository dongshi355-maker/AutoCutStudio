#!/usr/bin/env python3
"""
AutoCutStudio 🔵 独立版构建脚本
打包成自包含的 macOS .app，无需预先安装任何依赖。
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
DIST_DIR = PROJECT_DIR / "dist"
WORK_DIR = PROJECT_DIR / "build"

# ── PyInstaller 需要的额外资源 ──
EXTRA_FILES = [
    "hand_landmarker.task",   # 手势识别模型
]

# ── 从 Homebrew 复制 ffmpeg ──
FFMPEG_ORIG = "/opt/homebrew/bin/ffmpeg"
FFPROBE_ORIG = "/opt/homebrew/bin/ffprobe"


def find_ffmpeg():
    """查找系统 ffmpeg（支持 Intel/ARM Mac）"""
    for p in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]:
        if os.path.isfile(p):
            return p
    r = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def find_ffprobe():
    for p in ["/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe", "/usr/bin/ffprobe"]:
        if os.path.isfile(p):
            return p
    r = subprocess.run(["which", "ffprobe"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def copy_ffmpeg_bundle():
    """复制 ffmpeg 和 ffprobe 到打包资源目录"""
    ffmpeg_bin = find_ffmpeg()
    ffprobe_bin = find_ffprobe()
    if not ffmpeg_bin:
        print("❌ 未找到 ffmpeg，请先安装: brew install ffmpeg")
        sys.exit(1)

    bundle_ffmpeg_dir = PROJECT_DIR / "ffmpeg"
    bundle_ffmpeg_dir.mkdir(exist_ok=True)

    dest_ffmpeg = bundle_ffmpeg_dir / "ffmpeg"
    dest_ffprobe = bundle_ffmpeg_dir / "ffprobe"

    if not dest_ffmpeg.exists() or os.path.getmtime(ffmpeg_bin) != os.path.getmtime(dest_ffmpeg):
        shutil.copy2(ffmpeg_bin, dest_ffmpeg)
        print(f"  ✅ ffmpeg ({os.path.getsize(ffmpeg_bin)//1024}KB)")

    if ffprobe_bin and (not dest_ffprobe.exists() or os.path.getmtime(ffprobe_bin) != os.path.getmtime(dest_ffprobe)):
        shutil.copy2(ffprobe_bin, dest_ffprobe)
        print(f"  ✅ ffprobe ({os.path.getsize(ffprobe_bin)//1024}KB)")

    os.chmod(dest_ffmpeg, 0o755)
    os.chmod(dest_ffprobe, 0o755) if dest_ffprobe.exists() else None

    return str(bundle_ffmpeg_dir)


def main():
    print("="*50)
    print("🔵 AutoCutStudio 独立版构建工具")
    print("="*50)
    print()

    # 1. 检查依赖
    print("📦 检查环境...")
    py_path = shutil.which("pyinstaller")
    if not py_path:
        print("❌ 未找到 PyInstaller，正在安装...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller",
                       "--break-system-packages"], check=True)
        py_path = shutil.which("pyinstaller")
    print(f"  ✅ PyInstaller: {subprocess.run([py_path, '--version'], capture_output=True, text=True).stdout.strip()}")

    # 2. 准备 ffmpeg
    print()
    print("🎬 准备 ffmpeg...")
    ffmpeg_dir = copy_ffmpeg_bundle()

    # 3. 检查资源文件
    print()
    print("📁 检查资源文件...")
    for f in EXTRA_FILES:
        fp = PROJECT_DIR / f
        if not fp.exists():
            print(f"  ❌ 缺失: {f}")
            sys.exit(1)
        print(f"  ✅ {f}")

    main_script = PROJECT_DIR / "autocut_control.py"
    if not main_script.exists():
        print(f"  ❌ 缺失: autocut_control.py")
        sys.exit(1)
    print(f"  ✅ autocut_control.py")

    # 4. 构建
    print()
    print("🔨 构建中，这可能需要几分钟...")
    print()

    # 收集所有附加资源
    add_data = []
    for f in EXTRA_FILES:
        add_data.append(f"{PROJECT_DIR / f}{os.pathsep}.")
    # ffmpeg 目录
    add_data.append(f"{PROJECT_DIR / 'ffmpeg'}{os.pathsep}.")

    cmd = [
        py_path,
        "--noconfirm",
        "--clean",
        "--name", "AutoCutStudio",
        "--onefile",            # 单文件 .app（内部解压运行）
        "--windowed",           # 不显示控制台窗口
    ]
    for item in add_data:
        cmd += ["--add-data", item]
    cmd += [
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "mediapipe",
        "--hidden-import", "cv2",
        "--hidden-import", "numpy",
        "--hidden-import", "PIL",
        "--collect-all", "mediapipe",
        "--collect-all", "cv2",
        "--collect-all", "PIL",
        "--distpath", str(PROJECT_DIR / "dist"),
        "--workpath", str(PROJECT_DIR / "build"),
        "--specpath", str(PROJECT_DIR),
        str(main_script),
    ]

    # 如果有图标，指定它
    icon_path = PROJECT_DIR / "icon.icns"
    if icon_path.exists():
        cmd[cmd.index("--icon") + 1] = str(icon_path)

    print("  执行:")
    print(f"    {' '.join(str(c) for c in cmd[:8])} ...")
    print()

    result = subprocess.run(cmd, cwd=str(PROJECT_DIR))
    if result.returncode != 0:
        print()
        print("❌ 构建失败！")
        sys.exit(1)

    # 5. 清理临时文件
    print()
    app_path = DIST_DIR / "AutoCutStudio.app"
    if app_path.exists():
        print(f"✅ 构建成功！")
        print(f"   输出: {app_path}")
        print(f"   尺寸: {sum(f.stat().st_size for f in app_path.rglob('*')) // 1024 // 1024}MB")
        print()

    # 清理 ffmpeg 临时文件
    shutil.rmtree(PROJECT_DIR / "ffmpeg", ignore_errors=True)

    print("📋 使用说明:")
    print("   双击 AutoCutStudio.app 即可使用")
    print("   可复制到任意 Mac 上运行（macOS 12+）")
    print()

    # 可选：复制到桌面
    desktop_app = Path.home() / "Desktop" / "AutoCutStudio独立版.app"
    if not desktop_app.exists():
        if app_path.exists():
            try:
                shutil.copytree(str(app_path), str(desktop_app), symlinks=True)
                print(f"📋 已复制到桌面: {desktop_app}")
                print(f"   你也可以从 {app_path} 复制到任意位置")
            except Exception as e:
                print(f"  (桌面复制失败: {e})")


if __name__ == "__main__":
    main()
