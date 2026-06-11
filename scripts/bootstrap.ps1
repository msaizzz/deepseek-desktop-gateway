$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot\..

# ---------- 1. 拉取 vendor/litellm 源码（引用的开源库，未上传至本仓库） ----------
$vendorLitellm = "vendor/litellm"
if (-not (Test-Path "$vendorLitellm/pyproject.toml")) {
    Write-Host "正在拉取 LiteLLM 源码到 $vendorLitellm ..."
    if (Test-Path $vendorLitellm) {
        Remove-Item -Recurse -Force $vendorLitellm
    }
    git clone --branch litellm_internal_staging https://github.com/BerriAI/litellm.git $vendorLitellm
    Set-Location $vendorLitellm
    git checkout 6068bb7781b66ea51930f68ed8738ac46f0bdf7d
    Set-Location $PSScriptRoot\..
    Write-Host "LiteLLM 源码已就绪。"
} else {
    Write-Host "LiteLLM 源码已存在，跳过拉取。"
}

# ---------- 2. 创建虚拟环境 ----------
if (-not (Test-Path .venv)) {
    py -m venv .venv
}

. .\.venv\Scripts\Activate.ps1

# ---------- 3. 安装依赖（包含 -e ./vendor/litellm） ----------
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

Write-Host "环境初始化完成。请使用 'py -m src.deepseek_gateway.main' 启动程序。"
