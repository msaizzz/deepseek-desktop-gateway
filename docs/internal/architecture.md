# 架构说明

## 当前 MVP 组件

- `src/deepseek_gateway/gui.py`：桌面外壳、设置页面、自启动开关、报表导出入口。
- `src/deepseek_gateway/gateway_service.py`：LiteLLM Proxy 内核承载层，负责生成运行配置、托管 data-plane 路由并裁剪控制面。
- `src/deepseek_gateway/config_manager.py`：本地签名配置管理，并将上游密钥与签名密钥写入程序同目录、绑定当前 Windows 用户的 `secrets.json`。
- `src/deepseek_gateway/database.py`：SQLite 请求日志与月报导出。
- `src/deepseek_gateway/security.py`：管理员密码哈希与校验。
- `vendor/litellm`：本地 LiteLLM 源码，作为真正网关内核使用。

## 安全模型

- 程序会把上游 DeepSeek API Key 与签名密钥保存到程序同目录的 `secrets.json`。
- `secrets.json` 使用 Windows 当前用户绑定的受保护载荷保存敏感字段，不再以明文 JSON 值落盘。
- 如果目录被复制到另一台电脑，或由不同 Windows 账户启动，程序无法复用原来的上游 API Key，需要重新录入。
- 本地配置文件通过 HMAC 签名做完整性保护，避免被终端用户直接篡改。
- 一旦设置管理员密码，修改预算、上游地址、监听端口、上游密钥等敏感配置时都必须通过管理员认证。

## 当前 MVP 缺口

- 还没有 Windows 安装包。
- 还没有系统托盘菜单行为。
- 还没有集中式多设备汇总能力。
- 还没有首启初始化向导。
- 当前设备指纹主要依赖设备 ID 配置项，尚未增加更强的硬件指纹校验。

## 下一阶段计划

- 增加首启设置向导。
- 在图形界面中展示实时请求日志。
- 增加更丰富的 LiteLLM callback/guardrail 策略和超限预警阈值。
- 增加 Windows 打包、升级和异常恢复能力。
