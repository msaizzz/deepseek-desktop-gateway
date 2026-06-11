# VS Code 接入说明

## 1. 目标

本说明用于指导管理员将 SRW DeepSeek 本地桌面网关与 DeepSeek V4 for Copilot Chat 配合使用，并通过同一个 DeepSeek API Key 完成 LiteLLM 内核代理、费用统计和预算控制。

## 2. 前提条件

启用前请先准备：

- 已安装并可启动 SRW DeepSeek 本地桌面网关
- 已拿到可用的 DeepSeek API Key
- VS Code 中已安装 DeepSeek V4 for Copilot Chat 扩展

## 3. 网关侧配置

1. 启动 SRW DeepSeek 本地桌面网关。
2. 打开“设置”页。
3. 输入管理员密码。
4. 确认或修改监听地址，建议保持 `127.0.0.1`。
5. 确认或修改监听端口，建议保持 `8765`。
6. 填写月度预算（人民币），例如 `50.00`。
7. 按 DeepSeek 页面填写模型单价；当前桌面版支持分别配置缓存命中输入价、普通输入价和输出价。
8. 确认上游基础地址保持为 `https://api.deepseek.com`。
9. 填写企业统一使用的上游 API Key。
10. 点击“保存设置”。
11. 返回“概览”页，点击“启动网关”。

## 4. VS Code 侧配置

在 VS Code 的 `settings.json` 中加入以下配置：

```json
{
  "deepseek-copilot.baseUrl": "http://127.0.0.1:8765/v1",
  "deepseek-copilot.modelIdOverrides": {
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek-v4-pro"
  }
}
```

说明：

1. `baseUrl` 必须与网关概览页显示的本地地址一致。
2. 如果修改了监听端口，需要同步修改 `baseUrl` 中的端口。
3. 如果企业只开放了单一模型，也可以只保留一个 `modelIdOverrides` 条目。
4. 请求进入本地 LiteLLM 内核后，实际调用上游 DeepSeek 使用的是网关本地保存的 API Key，而不是 VS Code 扩展里单独维护的 Key。

## 5. 启用后如何确认生效

1. 在网关概览页确认状态为“运行中”。
2. 在 VS Code 中发起一次 Copilot Chat 请求。
3. 回到网关：
   - “概览”页中的本月费用会增加
   - “历史”页会逐月累计费用
   - “最近请求”窗口能看到刚才的调用记录

说明：当前桌面版会把缓存命中输入价、普通输入价和输出价同时写入 LiteLLM runtime config，本地预算和报表直接使用 LiteLLM 返回的 `response_cost`。

## 6. API Key 说明

1. DeepSeek API Key 不会明文写入配置文件。
2. 程序会把 API Key 保存到程序同目录的 `secrets.json`，并绑定到当前 Windows 用户。
3. 如果需要更换 API Key，直接在“设置”页输入新值并保存即可。
4. 如果把程序目录复制到另一台电脑，或改由其他 Windows 账户运行，需要重新填写 API Key。
5. 从实现上看，VS Code 扩展侧并不需要再提供真实的上游 API Key，因为真正向 DeepSeek 发请求的是本地 LiteLLM 内核。
6. 但如果 DeepSeek V4 for Copilot Chat 扩展首启界面强制要求先填写一个 Key 才允许继续，建议填一个任意非空占位值，例如 `gateway-placeholder`。这不会影响网关使用本地保存的真实 Key 转发请求。

## 7. 常见问题

### 7.1 VS Code 仍然直接访问上游，没有经过网关

请检查 `deepseek-copilot.baseUrl` 是否已指向 `http://127.0.0.1:8765/v1`。

### 7.2 网关启动了但请求仍失败

请依次检查：

1. DeepSeek API Key 是否填写正确
2. 本机监听端口是否被占用
3. 月度预算是否已超限
4. DeepSeek 上游接口当前是否可访问

### 7.3 如何更换默认管理员密码

当前内置默认管理员密码来自 [src/deepseek_gateway/security.py](src/deepseek_gateway/security.py) 中的 `DEFAULT_ADMIN_PASSWORD` 常量。修改代码后需要重新发包。
