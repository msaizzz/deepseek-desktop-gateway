# SRW DeepSeek 本地桌面网关

这是一个运行在 Windows 笔记本上的本地桌面网关，用于把 VS Code 中的 DeepSeek Copilot Chat 请求统一转发到本机代理，再由代理通过本地 LiteLLM 源码调用上游大模型接口，实现以下目标：

- 按设备统计输入 Token、输出 Token 和费用
- 对每台电脑设置月度费用上限
- 使用管理员密码保护关键配置
- 提供 CSV 和 XLSX 月报导出
- 提供历史月度消费查看、日志查看和数据备份恢复
- 支持开机自启动和本机固定监听地址

## 当前范围

当前版本是可运行的 MVP 骨架，已经具备以下能力：

- Windows 本地 OpenAI 兼容网关
- PySide6 图形界面
- 管理员密码保护配置
- SQLite 本地费用日志
- 按设备月度预算限制
- CSV 和 XLSX 报表导出

## 本地 LiteLLM 说明

本项目已经将 LiteLLM 源码落到本地目录：

- `vendor/litellm`

安装依赖时会优先从本地源码目录安装 LiteLLM，而不是从远程包仓库直接拉取已发布版本。这样做有三个好处：

1. 满足“LiteLLM 必须落到本地”的要求。
2. 后续如需修补 LiteLLM 行为，可以直接在本地源码基础上定制。
3. 部署时更容易审计版本来源和源码内容。

## 项目结构

- `src/deepseek_gateway`：桌面网关主代码
- `vendor/litellm`：本地 LiteLLM 源码
- `docs`：用户文档（使用说明、部署说明、运维指南、VS Code 接入说明）
- `docs/internal`：内部文档（架构说明、开发规范、发包说明）
- `requirements.txt`：开发与运行依赖
- `pyproject.toml`：项目打包元数据

## 快速启动

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
py -m src.deepseek_gateway.main
```

## VS Code 插件配置

请将 DeepSeek Copilot Chat 插件的请求指向本地网关：

```json
{
  "deepseek-copilot.baseUrl": "http://127.0.0.1:8765/v1",
  "deepseek-copilot.modelIdOverrides": {
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek-v4-pro"
  }
}
```

## 文档入口

### 面向终端用户（随 EXE 发布）

- `docs/部署说明.md` — 部署与迁移指南
- `docs/使用说明.md` — 用户操作手册
- `docs/运维指南.md` — 运维操作参考
- `docs/VS Code接入说明.md` — VS Code 插件配置

### 面向开发与发布人员

- `docs/internal/架构说明.md` — 系统架构设计
- `docs/internal/开发规范.md` — 开发纪律与约定
- `docs/internal/发包说明.md` — 打包与发布流程

## 当前注意事项

1. 管理员密码不会在启动界面弹窗展示，建议由发布管理员单独保管；如需修改默认管理员密码，位置见 [src/deepseek_gateway/security.py](src/deepseek_gateway/security.py)。
2. 源码开发模式下，上游 DeepSeek API Key 默认写入 Windows 凭据管理器；打包后的 EXE 模式会将其连同签名密钥写入程序同目录的 `secrets.json`，以支持整文件夹迁移。
3. 打包后的数据目录默认就是 EXE 所在目录，包含 `config.json`、`usage.db`、`secrets.json`、`logs`、`reports`、`runtime` 等文件与目录，整文件夹可直接复制迁移。
4. 配置文件带有完整性签名，直接修改配置文件会触发校验失败。
5. 当前已经支持历史月度消费记录查看、按月份导出报表，以及运维页中的日志查看、备份恢复和带密码的数据库重置。
6. 当前已经提供 Windows 发包脚本，发布后直接复制 `DeepSeekDesktopGateway.exe` 到其他电脑即可运行。
7. 当前 GUI、预算和报表中的金额口径统一按人民币展示与填写。

新增文档：

- `docs/VS Code接入说明.md`

## 一键发包

推荐直接运行 Python 一键发包脚本：

```powershell
py .\scripts\build_release.py
```

执行完成后，发布目录位于 `dist/release`。

当前打包脚本会在复制发布目录后自动执行发布前自检，确保：

- `tiktoken` 所需插件文件已经进入发布包
- `DeepSeekDesktopGateway.exe` 可以被成功拉起
