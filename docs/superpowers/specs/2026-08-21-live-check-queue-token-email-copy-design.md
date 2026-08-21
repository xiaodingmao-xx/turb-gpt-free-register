# 浏览器查活队列、Token 状态与邮箱复制设计

## 1. 目标

在默认新版 WebUI 中增加以下能力：

1. 实时展示 Roxy 浏览器查活队列：当前执行账号、排队账号数量、延迟重试数量和可用 worker。
2. 浏览器查活完成后，账号页及时刷新 Token 是否存在、查活状态和最近更新时间，无需用户手动刷新页面。
3. 在账号页和邮箱池页的邮箱列增加“复制邮箱名称”，复制完整邮箱地址。
4. 细化查活日志。遇到 HTTP 403 等失败时，日志必须指出失败阶段、请求类型、页面/接口、网络线路、profile 状态和脱敏响应摘要，不能只显示状态码。

本次只修改默认新版界面，忽略 `index_legacy.html`。

## 2. 现有能力与缺口

项目已有：

- `core/live_check_service.py` 的 protocol/browser 查活分发、浏览器独立 worker、数据库状态写回和延迟重试。
- `db.update_account_liveness()` 在浏览器查活成功后写回新的 `access_token`，失败时保留原 Token。
- `webui/app.py` 的批量查活接口和账号状态压缩响应。
- `webui/templates/index.html` 的浏览器查活按钮、查活日志弹窗、账号页和邮箱池页。

当前缺口：

- `queue_settings()` 只有配置容量，没有从账号状态计算当前执行账号和排队数量。
- 浏览器查活提交后，前端没有统一轮询所有已提交账号的状态，Token 状态更新依赖手动刷新或单个日志弹窗。
- 邮箱列没有直接复制完整邮箱地址的按钮；邮箱池现有“复制邮箱”动作复制的是整行素材。
- 查活日志只适合展示通用错误摘要，无法定位 403 发生在哪个阶段和请求。

## 3. 方案

采用轻量 HTTP 轮询，不引入 WebSocket/SSE。

```text
浏览器查活 worker
  -> 更新账号 queued/running/live/failed
  -> 写入结构化阶段日志
  -> 成功写回 access_token

新版账号页
  -> POST /api/accounts/check-live-bulk
  -> 每 2 秒 GET /api/accounts/live-check-status
  -> 合并账号轻量状态并刷新 Token/查活显示
  -> 同时刷新浏览器队列摘要
```

理由：查活任务状态已经持久化在账号记录中，轮询可以复用现有 Flask API、分页和状态恢复逻辑；SSE/WebSocket 对本地单机 WebUI 的收益不足以抵消连接生命周期和重连处理成本。

## 4. 队列状态接口

### 4.1 服务层

在 `core/live_check_service.py` 增加浏览器队列快照函数，建议接口：

```python
def queue_status(mode: str = "browser") -> dict:
    """返回指定查活后端的容量、当前执行和排队快照。"""
```

浏览器模式返回：

```json
{
  "backend": "browser",
  "workers": 1,
  "queue_limit": 100,
  "active": 1,
  "queued": 5,
  "waiting": 4,
  "delayed": 1,
  "available_workers": 0,
  "running_accounts": [
    {
      "id": 146,
      "email": "pouch.70-guzzler@icloud.com",
      "started_at": "2026-08-21T12:00:00",
      "attempt": 1,
      "max_attempts": 3
    }
  ],
  "positions": {"147": 1, "148": 2}
}
```

计算规则：

- `running_accounts` 来自 `live_check_status=running` 且 `live_check_backend=browser` 的账号。
- `queued` 来自 `live_check_status=queued` 且 backend 为 browser 的账号，包含延迟重试。
- `delayed` 按 `live_check_next_retry_at` 是否晚于当前时间计算。
- `waiting` 为可立即排队的 queued 数量。
- `positions` 只为 waiting 队列生成，按 `live_check_queued_at`、账号 ID 稳定排序。
- 展示字段只允许账号 ID、邮箱、时间和次数，不返回 Token、密码、Cookie 或 profile 的敏感字段。

protocol 模式继续兼容现有 `queue_settings()` 行为；新增状态函数不改变 protocol 默认查活逻辑。

### 4.2 Web API

在 `webui/app.py` 增加：

```text
GET /api/accounts/live-check-status?ids=146,147&mode=browser
```

返回：

```json
{
  "ok": true,
  "mode": "browser",
  "items": [
    {
      "id": 146,
      "email": "pouch.70-guzzler@icloud.com",
      "has_access_token": true,
      "live_check_status": "running",
      "live_check_backend": "browser",
      "live_check_attempt": 1,
      "live_check_max_attempts": 3,
      "live_check_error": null,
      "live_checked_at": null
    }
  ],
  "queue": {}
}
```

接口只返回账号页已有的轻量字段，并通过 `live_check_service.queue_status()` 返回队列摘要。`ids` 为空时只返回队列摘要；非法 mode 返回 HTTP 400。

批量入队接口的返回值继续保留已有字段，并将完整队列快照放入 `queue`，便于前端立即显示。

## 5. Token 状态及时更新

### 5.1 后端写回

保留现有成功/失败语义：

- 成功：`db.update_account_liveness()` 写入最新 access token、session 用户信息、套餐信息和查活时间。
- 失败：不覆盖、不清空已有 access token，只写入失败状态、failure kind、错误摘要和查活时间。
- 403、Cloudflare、网络超时、浏览器启动失败、OTP 超时都不能直接标记为 `deactivated`。

### 5.2 前端轮询

在 `index.html` 增加浏览器查活轮询集合和定时器：

- `checkSelectedLive(..., "browser")` 成功入队后，将 `started` 中的账号 ID 加入轮询集合。
- 每 2 秒请求 `/api/accounts/live-check-status`。
- 将返回的轻量字段合并到 `ACCOUNTS`，重新渲染账号表格和队列摘要。
- 每个账号不再是 queued/running 后，从轮询集合移除；所有账号结束后清理定时器。
- 轮询请求设置 in-flight 保护，防止上一次请求未返回时重复发起。
- 页面重新加载账号列表时同步读取最新 Token 状态；不在前端返回或缓存完整 Token。

账号页队列摘要至少显示：

```text
浏览器查活：执行 pouch.70-guzzler@icloud.com · 排队 5
```

多 worker 时用逗号显示多个正在执行的账号；无运行任务时显示“执行 空闲”。

## 6. 复制邮箱名称

### 6.1 账号页

在 `index.html` 的账号邮箱单元格中增加按钮：

```html
<button type="button" data-account-copy-email="146">复制邮箱名称</button>
```

点击后复制当前行的完整 `email` 值，成功提示“邮箱名称已复制”，失败提示“复制邮箱名称失败”。不调用 secret API，因为账号列表已经安全返回邮箱地址。

### 6.2 邮箱池页

在邮箱池邮箱单元格中增加：

```html
<button type="button" data-pool-copy-email="pouch.70-guzzler@icloud.com">复制邮箱名称</button>
```

点击后复制完整邮箱地址。保留现有“复制邮箱”整行素材动作，避免改变已有使用习惯。

按钮事件使用现有事件委托和 `copyText()`，并对 HTML 属性继续使用 `esc()`，防止邮箱内容破坏模板。

## 7. 详细查活日志

### 7.1 目标

日志必须能回答：

1. 任务处于哪个阶段；
2. 请求了哪个 host/path 或页面；
3. 使用了哪种网络线路和脱敏出口；
4. 使用了保存 profile 还是临时 profile；
5. 失败是 HTTP 状态、页面状态、网络异常还是身份校验失败；
6. 是否重试、当前第几次、下一次重试时间是什么。

### 7.2 统一日志字段

浏览器查活日志沿用 `[浏览器查活]` 前缀，并使用稳定字段：

```text
12:00:01 [INFO] [浏览器查活] phase=profile_open account_id=146 profile_source=saved profile_hint=<redacted>
12:00:03 [INFO] [浏览器查活] phase=page_load url=https://chatgpt.com/ status=started
12:00:05 [INFO] [浏览器查活] phase=session_probe request=GET /api/auth/session http_status=403 route=direct proxy=-
12:00:05 [WARN] [浏览器查活] phase=session_probe failure_kind=network_unavailable http_status=403 response_summary="Cloudflare challenge or access denied" retryable=true
12:00:05 [INFO] [浏览器查活] phase=retry attempt=1/3 delay=15s next_retry_at=...
```

至少覆盖以下阶段：

- `queue`: 入队、队列位置、触发来源；
- `profile_open`: profile 来源、profile ID 脱敏摘要、打开结果；
- `driver_start`: driver 启动/初始化结果；
- `page_load`: 页面 host/path、加载结果和耗时；
- `session_probe`: `/api/auth/session` 请求结果、HTTP 状态、响应结构摘要；
- `login_start`: 是否进入登录流程；不记录密码、Cookie、Token 或其他认证数据，邮箱只按账号标识的既有规则记录；
- `otp_wait`: OTP 等待开始/结束、attempt 和超时，不记录 OTP 值；
- `callback`: callback 页面 host/path 和结果，不记录 query 中的 code/state；
- `session_validate`: session 邮箱/user id/token exp 校验结果，不记录 Token；
- `token_persist`: Token 写回成功/失败，只记录字段名和结果；
- `cleanup`: driver/profile 关闭和临时 profile 清理结果；
- `retry` / `terminal`: 重试决策、failure kind、最终状态。

### 7.3 HTTP 403 诊断

HTTP 403 不能只落成 `HTTP 403`。日志至少包含：

- `phase`：例如 `providers`, `csrf`, `signin`, `session_probe`；
- `request`：HTTP 方法和脱敏 path；
- `host`：只保留域名；
- `http_status=403`；
- `route`：`direct` 或 `proxy`；
- `proxy`：只保留脱敏地址，不保留用户名和密码；
- `profile_source`：`saved` 或 `temporary`；profile 只记录脱敏摘要，不记录可还原的完整标识；
- `response_summary`：从响应状态、Content-Type、页面标题和前 160 个字符生成，去除 HTML、Cookie、Token 和 OAuth 参数；
- `failure_kind`：如 `cloudflare_blocked`、`access_denied`、`session_missing`；
- `retryable`：明确是否会自动重试。

浏览器驱动异常也要记录异常类型、阶段和脱敏消息；不得把完整 Selenium stack trace、Cookie、请求头或响应体原样写入用户日志。

### 7.4 敏感信息规则

禁止写入：

- access token、JWT、refresh token；
- Cookie、Authorization、代理认证信息；
- OTP 明文；
- OAuth callback 的 `code`、`state`、完整 URL query；
- 注册密码、账号密码。

允许写入：

- account id、邮箱；
- host 和 path；
- HTTP 状态码和 Content-Type；
- 脱敏 profile 摘要；
- 脱敏代理摘要；
- response summary 的安全文本；
- 阶段、耗时、重试次数和 failure kind。

需要复用或新增统一的 URL/错误脱敏函数，保证浏览器查活调用链不会把 callback URL 或响应认证字段原样写入日志。

## 8. 文件边界

修改：

- `core/live_check_service.py`：增加浏览器队列快照和状态展示字段。
- `core/roxy_live_check.py`：补充阶段日志、HTTP 诊断和响应脱敏。
- `webui/app.py`：增加查活状态轮询接口。
- `webui/templates/index.html`：队列状态、Token 状态轮询、账号/邮箱池复制邮箱名称按钮。
- `tests/test_live_check_browser_service.py`：队列快照、状态轮询数据、日志字段测试。
- `tests/test_roxy_live_check.py`：403 诊断和敏感信息脱敏测试。
- `tests/test_webui_account_features.py`：接口和新版模板行为测试。

不修改：

- `webui/templates/index_legacy.html`；
- protocol 查活默认行为；
- Token 失败时保留旧值的数据库语义；
- 账号/邮箱池既有复制整行和复制取件地址功能。

## 9. 测试策略

### 9.1 服务层

- browser 队列返回 running 账号邮箱和 queued 数量；
- 延迟重试同时出现在 `queued` 和 `delayed`，但不占 waiting 位置；
- 多 worker 返回多个运行账号；
- protocol 模式仍保持原有配置接口。

### 9.2 浏览器日志

- HTTP 403 日志包含 phase、request、http_status、route、failure_kind 和 retryable；
- providers/session/callback 等阶段名称正确；
- response summary 被截断且不包含 Cookie、Token、OTP、code、state；
- profile、代理和异常信息均脱敏；
- 失败日志只更新诊断，不覆盖旧 Token。

### 9.3 WebUI

- 查活状态接口返回账号轻量状态和 queue；
- 非法 mode 返回 400；
- 新版模板包含账号页和邮箱池页“复制邮箱名称”；
- 前端源码包含 2 秒轮询、状态合并和任务结束清理逻辑；
- 旧版模板不纳入本次修改范围。

## 10. 验收标准

1. 点击浏览器查活后，账号页立即显示当前执行账号和排队数量。
2. 队列变化能在约 2 秒内反映到新版账号页。
3. 浏览器查活成功后，Token 状态、查活状态和时间自动更新，无需手动刷新。
4. 浏览器查活失败时旧 Token 保留。
5. HTTP 403 日志能定位阶段、接口、网络线路、profile 和重试决策，而不是只有状态码。
6. 日志中没有 Token、Cookie、OTP、OAuth code/state、密码或代理凭据。
7. 账号页和邮箱池页邮箱列都有“复制邮箱名称”，复制结果是完整邮箱地址。
8. 旧版界面不做兼容改动。
9. 新增测试和相关回归测试全部通过。
