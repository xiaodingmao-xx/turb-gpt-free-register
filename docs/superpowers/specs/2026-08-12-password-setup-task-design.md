# 密码设置任务功能设计

## 目标

为 RoxyBrowser 注册项目增加统一的 ChatGPT 密码设置任务，支持：

- 注册成功后自动设置密码；
- 从账号列表手动选择账号执行；
- 添加密码和修改密码两种模式；
- 固定密码和每账号随机密码两种来源；
- 自动获取邮箱 OTP，自动获取失败后手动输入；
- 使用当前账号的 RoxyBrowser 环境完成同源认证和密码提交。

本功能只处理用户已授权账号的密码管理，不通过 `feiyangka.com` 页面跨域调用 ChatGPT 接口。

## 设计原则

1. 密码设置是独立任务，不覆盖注册任务状态。
2. 所有密码设置任务进入统一队列，Roxy 密码操作并发数由 `ROXY_PASSWORD_SETUP_WORKERS` 控制，默认串行。
3. 密码设置过程始终在 `chatgpt.com` 页面上下文执行 CSRF、重新认证和密码提交，避免浏览器 CORS 限制。
4. 任务失败不删除已注册账号，不改变账号的有效状态。
5. 密码和 OTP 不写入日志；密码默认不通过账号列表接口返回。

## 入口

### 注册后自动入口

注册流程在成功拿到 session/accessToken 后，根据配置创建密码任务。自动入口默认关闭，开启后使用配置的模式和密码来源。

注册任务与密码任务的关系：

```text
注册成功 → 创建密码任务 → 密码任务完成/失败 → 保存注册结果
```

如果密码任务失败，账号仍保存为注册成功账号，密码任务单独显示失败并提供重试。

### 账号列表手动入口

账号列表支持勾选一个或多个账号，点击“设置密码”。弹窗允许选择：

- 操作：添加密码 / 修改密码；
- 密码来源：固定密码 / 每账号随机生成；
- 验证码方式：自动获取，失败后手动输入。

## 任务模型

密码任务使用独立记录，建议字段：

```text
id
account_id
email
mode              add / reset
password_source   fixed / random
status            queued / running / waiting_otp / success / skipped / failed / cancelled
error
password_set_at
created_at
completed_at
```

状态含义：

- `queued`：等待执行；
- `running`：正在使用 Roxy 环境；
- `waiting_otp`：等待自动获取或手动输入邮箱验证码；
- `success`：密码设置完成；
- `skipped`：添加模式检测到已有密码，未重复操作；
- `failed`：流程失败，可重试；
- `cancelled`：用户取消。

## 接口

创建任务：

```http
POST /api/accounts/password-setup
```

```json
{
  "account_ids": [12, 15],
  "mode": "add",
  "password_source": "random",
  "manual_otp": false
}
```

查询状态：

```http
GET /api/accounts/password-setup-status
```

取消任务：

```http
POST /api/accounts/password-setup/cancel
```

重试任务：

```http
POST /api/accounts/password-setup/retry
```

重试必须重新创建认证流程，不复用过期的 authorize URL、CSRF token 或 OTP。

## Roxy 执行流程

```text
检查账号锁
  ↓
打开/创建账号 Roxy 环境
  ↓
访问 chatgpt.com
  ↓
同源 GET /api/auth/csrf
  ↓
同源 POST /api/auth/signin/openai
  ↓
打开 authorize URL
  ↓
邮箱 OTP
  ├─ 自动邮箱池获取
  └─ 失败后页面手动输入
  ↓
提交 OTP
  ↓
进入 reset-password/new-password
  ↓
填写并提交密码
  ↓
刷新 session，保存结果
```

添加模式使用 `post_login_add_password`，修改模式使用 `post_login_password_reset`。

## 密码来源

### 固定密码

从 `.env` 中读取，不写入任务日志。适合人工明确指定同一密码的场景。

### 随机密码

每个账号生成一次强随机密码。任务重试时复用该任务已生成的密码，避免一次任务产生多个未知密码。成功后保存到账号的 `registration_password` 字段。

配置优先级：

```text
任务指定密码来源
  ↓
ROXY_PASSWORD_SETUP_PASSWORD
  ↓
REGISTER_PASSWORD
  ↓
每账号随机生成
```

## 并发和锁

- 同一账号不能同时存在两个未完成密码任务；
- 同一邮箱同一时间只允许一个 OTP 流程；
- Roxy 环境创建使用全局串行锁；
- 密码任务默认一次只运行一个任务，配置为 2 或更高时允许多个独立环境同时执行；
- 其他类型任务不应复用正在进行密码设置的 Roxy 环境。

Roxy 创建保持串行；密码设置阶段按配置并发执行，且每个注册线程继续独占自己的 Selenium driver，避免跨线程操作浏览器。

## 重试与错误处理

- Roxy 创建失败：最多自动重试 2 次；
- 页面加载失败：最多自动重试 2 次；
- OTP 获取失败：进入 `waiting_otp`，允许用户手动继续；
- OTP 错误或过期：最多重新获取 3 次；
- 密码提交失败：不自动重复提交，改为人工点击重试；
- 已完成任务不重复执行，除非用户明确选择修改密码；
- 失败只更新密码任务，不删除账号、不标记账号废号。

## 页面设计

账号列表新增“设置密码”批量操作。任务区域展示：

```text
账号、操作模式、密码来源、状态、错误原因、创建时间、完成时间、操作
```

`waiting_otp` 任务显示验证码输入框、“继续执行”和“取消任务”。

账号密码列保持脱敏显示，点击“显示密码”时才通过受保护接口读取明文。

## 安全和审计

- 密码、OTP、CSRF token、authorize URL 不写入日志；
- 日志只记录密码模式、密码长度和任务状态；
- 固定密码仅保存到 `.env`；
- API 列表默认只返回 `password_set`，不返回密码明文；
- 所有任务保留创建、执行、完成和失败时间；
- 任务日志按账号隔离，便于排查但不泄露敏感值。

## 测试计划

1. 测试添加模式生成 `post_login_add_password` 请求参数；
2. 测试修改模式生成 `post_login_password_reset` 请求参数；
3. 测试非法模式被拒绝；
4. 测试已有密码时添加模式跳过；
5. 测试固定密码和随机密码优先级；
6. 测试同一账号重复任务被拒绝；
7. 测试 OTP 自动获取失败后进入 `waiting_otp`；
8. 测试任务重试会创建新的认证流程；
9. 测试 Roxy 串行锁；
10. 测试 API 不返回密码和 OTP 明文。

## 配置建议

```env
ROXY_PASSWORD_SETUP_ENABLED="False"
ROXY_PASSWORD_SETUP_MODE="post_login_add_password"
ROXY_PASSWORD_SETUP_PASSWORD=""
ROXY_PASSWORD_SETUP_TIMEOUT="120"
ROXY_PASSWORD_SETUP_WORKERS="1"
ROXY_PASSWORD_SETUP_QUEUE_LIMIT="100"
```

默认关闭自动入口，避免配置保存后意外批量修改账号密码。`ROXY_PASSWORD_SETUP_WORKERS=1` 保持串行；设置为 `2` 或更高时，多个注册线程可以在各自的 Roxy 环境中同时执行密码修改。Roxy 环境创建接口仍保持串行，Selenium driver 不跨线程传递。
