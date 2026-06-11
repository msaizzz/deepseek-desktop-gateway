from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

from verify_release_build import verify_release_or_raise


ROOT_DIR = Path(__file__).resolve().parents[1]
VENV_DIR = ROOT_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
DIST_DIR = ROOT_DIR / "dist"
RELEASE_DIR = DIST_DIR / "release"
RELEASE_EXE = RELEASE_DIR / "DeepSeekDesktopGateway.exe"


def run_command(command: list[str]) -> None:
    print("执行命令:", " ".join(command))
    subprocess.run(command, cwd=ROOT_DIR, check=True)


def stop_running_release_process() -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", "DeepSeekDesktopGateway.exe", "/T"],
        cwd=ROOT_DIR,
        check=False,
        capture_output=True,
        text=True,
    )


def remove_tree(path: Path) -> None:
    for attempt in range(5):
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if attempt == 4:
                raise
            time.sleep(1)


def ensure_venv() -> None:
    if VENV_PYTHON.exists():
        return
    print("未检测到 .venv，开始创建虚拟环境。")
    run_command([sys.executable, "-m", "venv", str(VENV_DIR)])


def install_dependencies() -> None:
    run_command([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"])
    if _pyinstaller_ready():
        print("核心构建依赖已就绪，跳过重装。")
        return
    run_command([str(VENV_PYTHON), "-m", "pip", "install", "-r", "requirements.txt"])
    run_command([str(VENV_PYTHON), "-m", "pip", "install", "pyinstaller"])


def _pyinstaller_ready() -> bool:
    try:
        import PyInstaller  # noqa: F401
        import litellm      # noqa: F401
        return True
    except ImportError:
        return False


def clean_outputs() -> None:
    stop_running_release_process()
    for path in [ROOT_DIR / "build", RELEASE_DIR, DIST_DIR / "DeepSeekDesktopGateway.exe"]:
        if path.exists():
            print(f"清理: {path}")
            if path.is_dir():
                remove_tree(path)
            else:
                path.unlink()
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)


def build_executable() -> None:
    run_command([str(VENV_PYTHON), "-m", "PyInstaller", "-y", "--clean", "--distpath", str(RELEASE_DIR), "packaging\\pyinstaller.spec"])


def copy_release_files() -> None:
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    built_exe = RELEASE_EXE
    if not built_exe.exists():
        raise FileNotFoundError(f"PyInstaller 未产出预期 EXE: {built_exe}")

    # 仅复制面向终端用户的文档，内部文档（架构说明、发包说明、开发规范等）不随 EXE 发布
    USER_FACING_DOCS = [
        "使用说明.md",
        "部署说明.md",
        "运维指南.md",
        "VS Code接入说明.md",
    ]
    for doc_name in USER_FACING_DOCS:
        doc_file = ROOT_DIR / "docs" / doc_name
        if not doc_file.exists():
            print(f"警告: 文档 {doc_name} 不存在，跳过。")
            continue
        target = RELEASE_DIR / doc_name
        print(f"复制文档: {doc_name}")
        shutil.copy2(doc_file, target)


def verify_release() -> None:
    print("执行发布前自检。")
    summary = verify_release_or_raise(ROOT_DIR)
    print("发布前自检通过。")
    print(f"插件文件: {summary['dist_file']}")
    print(f"EXE 检查: {summary['exe_file']}")


def main() -> int:
    print("开始生成 SRW DeepSeek 本地桌面网关发布包。")
    ensure_venv()
    install_dependencies()
    clean_outputs()
    build_executable()
    copy_release_files()
    verify_release()
    print(f"发布包生成完成: {RELEASE_DIR}")
    print(f"可直接双击 {RELEASE_EXE} 启动图形界面。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())