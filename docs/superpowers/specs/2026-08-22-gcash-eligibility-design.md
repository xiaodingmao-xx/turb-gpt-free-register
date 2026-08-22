# GCash 账号资格检测设计

## 目标

在当前账号页面增加一个显式触发的“查 GCash 资格”任务。任务只使用当前账号已有的 ChatGPT `access_token`，创建未确认的结算会话并读取返回的支付方式能力，判断当前菲律宾 PHP 结算条件下是否出现 GCash。任务永远不确认 Checkout、不创建 PaymentMethod、不提交付款。

## 判断边界

“账号有 GCash 资格”拆成两个结果，禁止合并成一个模糊的布尔值：

1. `gcash_available`：当前结算会话的支付方式列表包含 `gcash` 或 `external_gcash`。
2. `trial_eligible`：Checkout 响应明确返回账号可试用且实际试用字段生效。该字段只表示试用资格，不代表 GCash 可用。

最终状态使用以下枚举：`available`、`unavailable`、`trial_ineligible`、`already_paid`、`credential_invalid`、`unknown`。网络错误、Cloudflare、429、Checkout 创建失败和 Stripe 初始化失败都必须归为 `unknown`，不能显示为“没有资格”。

## 请求约束

- 结算地区固定为 `PH`，货币固定为 `PHP`。
- 复用项目现有套餐查询的代理解析、超时、重试和脱敏日志策略。
- 请求只允许到 ChatGPT Checkout 和 Stripe 初始化流程；不调用确认支付、创建 PaymentMethod 或付款接口。
- 不保存 Checkout Session ID、Stripe publishable key、完整响应、Token 或 Cookie；数据库只保存派生判断结果。
- 默认关闭自动检测，只允许用户在当前 WebUI 显式点击后入队。
- 只修改新版 `webui/templates/index.html`，不维护 `index_legacy.html`。

## 结果字段

任务返回并持久化以下派生字段：

- `gcash_check_status`、`gcash_check_ok`、`gcash_check_error`
- `gcash_checked_at`、`gcash_decision`
- `gcash_available`、`gcash_trial_eligible`、`gcash_actual_trial`
- `gcash_payment_methods`、`gcash_payment_method_status`
- `gcash_currency`、`gcash_amount_due`、`gcash_stripe_mode`
- `gcash_http_status`
- `gcash_network_route`、`gcash_proxy_used`、`gcash_proxy_ip`

`gcash_payment_methods` 只保存规范化后的支付方式名称列表。

## WebUI 行为

- 账号页增加“查 GCash 资格”单账号和批量操作。
- 账号页显示“GCash：可用/不可用/待检测/检测中/未知/凭据失效/已订阅”。
- 显示检测队列的运行数、排队数和当前执行账号。
- 支持查看每个账号最近一次 GCash 检测日志。
- 前端轮询只接收状态和派生字段，不接收 Token、Cookie、Checkout Session ID 或完整响应。
