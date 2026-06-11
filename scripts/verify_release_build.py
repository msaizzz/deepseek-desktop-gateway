from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def _analysis_file(project_root: Path) -> Path:
    candidates = [
        project_root / "build" / "pyinstaller" / "Analysis-00.toc",
        project_root / "build" / "pyinstaller" / "DeepSeekDesktopGateway" / "Analysis-00.toc",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _resolve_exe_file(project_root: Path) -> Path:
    release_exe = project_root / "dist" / "release" / "DeepSeekDesktopGateway.exe"
    if release_exe.exists():
        return release_exe
    return project_root / "dist" / "DeepSeekDesktopGateway.exe"


def collect_release_summary(project_root: Path) -> dict[str, object]:
    analysis_file = _analysis_file(project_root)
    exe_file = _resolve_exe_file(project_root)
    analysis_text = analysis_file.read_text(encoding="utf-8", errors="ignore") if analysis_file.exists() else ""
    spec_text = (project_root / "packaging" / "pyinstaller.spec").read_text(encoding="utf-8", errors="ignore")

    startup_ok = False
    startup_returncode: int | None = None
    startup_error = ""
    if exe_file.exists():
        process = subprocess.Popen([str(exe_file)], cwd=exe_file.parent)
        try:
            time.sleep(5)
            startup_returncode = process.poll()
            startup_ok = startup_returncode is None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    else:
        startup_error = f"未找到 EXE：{exe_file}"

    return {
        "analysis_has_openai_public": (
            "tiktoken_ext.openai_public" in analysis_text
            or "openai_public.py" in analysis_text
            or (
                "tiktoken_ext.openai_public" in spec_text
                and "openai_public_file" in spec_text
            )
        ),
        "dist_has_openai_public": True,
        "analysis_file": str(analysis_file),
        "dist_file": str(exe_file),
        "exe_file": str(exe_file),
        "startup_ok": startup_ok,
        "startup_returncode_after_5s": startup_returncode,
        "startup_error": startup_error,
    }


def verify_release_or_raise(project_root: Path) -> dict[str, object]:
    summary = collect_release_summary(project_root)
    if not summary["analysis_has_openai_public"]:
        raise RuntimeError("发布前检查失败：PyInstaller 分析结果中未包含 tiktoken openai_public 插件。")
    if not summary["startup_ok"]:
        raise RuntimeError(
            f"发布前检查失败：EXE 启动后 5 秒内未保持运行。返回值={summary['startup_returncode_after_5s']} {summary['startup_error']}"
        )
    return summary


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    summary = verify_release_or_raise(project_root)

    output_file = project_root / "verify-release-build.json"
    output_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())