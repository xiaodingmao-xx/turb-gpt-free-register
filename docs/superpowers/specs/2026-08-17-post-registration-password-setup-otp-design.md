# 注册后设置密码 OTP 隔离与后台续跑设计

日期：2026-08-17
状态：已获用户批准，等待实施计划

## 1. 背景与问题

任务 1050 已完成账号注册并取得 `accessToken`，随后在当前 Roxy 浏览器中执行
`post_login_add_password`。注册阶段使用的验证码为 `119006`；设置密码阶段又收到
了新的 `119006` 邮件。

日志证明本次设置密码失败并不是验证码被页面拒绝：

- 16:14:02 设置密码流程点击了 `Resend email`；
- 16:15:32 GenericAPI 发现触发时间之后的新邮件，并锁定候选 `119006`；
- 当时总等待时间只剩约 1 秒，无法完成额外 5 秒 settle；
- 16:15:35 取码函数以“候选的 settle 未完成”抛出超时，候选验证码从未提交给页面。

截图中 16:14 连续出现两封同码邮件，说明 OpenAI 可能在同一验证码有效期内重复投递
相同数字。系统必须区分“邮件是否为本轮新投递”和“验证码字符串是否与上轮相同”，
不能把验证码值当成消息身份。

当前实现还有两个耦合问题：

1. 设置密码授权流程本身可能已经触发邮件，但代码因为存在注册验证码而立即再次点击
   Resend，造成重复邮件和额外限流风险。
2. GenericAPI 把搜索新邮件的总超时和候选 settle 共用同一个截止时间；候选在截止时间
   前出现，仍可能因为没有剩余 settle 时间而失败。

## 2. 目标与非目标

### 2.1 目标

- 注册结果与设置密码结果解耦：注册成功后，设置密码失败不得回滚或丢弃账号。
- 设置密码使用独立 OTP 挑战，不复用注册阶段的基线和触发时间。
- 同码的新邮件可以被接受；触发前的缓存邮件必须继续被拒绝。
- 候选在搜索截止前出现时，必须获得完整 settle 确认窗口。
- 优先在当前浏览器中即时设置密码；即时失败后，账号保存并自动进入后台队列续跑。
- 后台任务在注册临时 Roxy 环境清理完成后才启动，避免打开与删除同一 profile 的竞态。
- 所有状态和最终错误可在账号页追踪，并保留手动重新入队能力。

### 2.2 非目标

- 不改变 OpenAI 服务端验证码的生成或有效期规则。
- 不保证外部邮件服务永久不可用时仍能自动成功。
- 不把设置密码失败视为注册失败。
- 本次不实现跨进程持久化调度器；服务重启时沿用现有中断恢复规则，账号仍保留并可重新入队。
- 不进行与 OTP、设置密码队列无关的 Roxy 或账号页重构。

## 3. 方案选择

采用“当前浏览器即时设置 + 失败后后台续跑”的混合方案。

未采用的方案：

- 仅增加 `OTP_MAX_WAIT`：只能降低复现概率，不能修复 settle 截止时间冲突和重复发信。
- 全部改为后台设置：隔离彻底，但每个账号都要再次打开环境并重新认证，耗时和资源成本更高。

## 4. OTP 挑战模型

每次会触发邮件的操作都建立独立挑战上下文。概念字段如下：

```text
OtpChallengeContext
  purpose          registration | password_setup
  attempt          当前挑战轮次
  baseline         触发前 GenericAPI 观察快照
  triggered_at     本轮触发动作之前记录的 Unix 时间戳
  previous_code    上一业务阶段提交过的验证码，仅作诊断
```

现有 `OtpBaseline` 继续保存 `codes`、`message_ids` 和 `captured_at`。设置密码流程不得
沿用注册阶段的 baseline，而是在设置密码授权动作之前重新调用
`capture_otp_baseline(email)`。

### 4.1 新邮件判定

GenericAPI 候选满足以下任一条件时，视为本轮新邮件：

1. `message_id` 不在本轮 baseline 的 `message_ids` 中；
2. 可解析的 `msg_ts >= triggered_at - 2 秒`；
3. baseline 中没有相同验证码，并且服务确实无法提供消息 ID 和时间戳。

候选满足以下条件时继续等待：

- 消息 ID 仍属于 baseline；或
- 邮件时间早于本轮触发时间；或
- 服务没有消息 ID/时间戳，且验证码仍与 baseline 相同，无法证明发生了新投递。

验证码字符串相同本身不是拒绝条件。新消息 ID 或新时间戳足以证明重新投递，因此注册
阶段和设置密码阶段都可以使用相同的六位数字。

设置密码流程不使用 `exclude_codes` 排除注册阶段的验证码值。各邮箱来源使用自己的
`after_ts` 新鲜度规则；GenericAPI 额外使用 baseline、消息 ID 和时间戳证明发生了新投递。
`exclude_codes` 仍保留给“页面已经明确拒绝某个验证码”的注册阶段重试，不承担跨业务
阶段区分验证码的职责。

## 5. 设置密码即时流程

### 5.1 首次挑战

顺序必须是：

1. 确定设置密码模式和目标密码；
2. 抓取设置密码专用 OTP baseline；
3. 记录 `triggered_at`；
4. 调用 `_fetch_password_setup_authorize_url` 并打开授权 URL；
5. 确认进入邮箱验证码页；
6. 直接等待本轮新邮件，不立即点击 Resend；
7. 获得候选后提交 OTP，并根据页面状态判断 accepted/invalid/advanced；
8. accepted 后填写并提交新密码。

把 baseline 和 `triggered_at` 放在 authorize 请求之前，是因为 authorize 请求或随后导航
均可能触发首封邮件。

### 5.2 重发挑战

只有以下情况可以点击 Resend：

- 首次挑战在搜索期限内没有出现新邮件；
- 页面明确判定提交的 OTP 无效或过期。

每次重发必须重新执行：

1. 抓取当前 baseline；
2. 记录新的 `triggered_at`；
3. 点击 Resend；
4. 等待 baseline 之后的新消息；
5. 提交并检查页面结果。

不能只更新 `after_ts` 而保留旧 baseline，也不能在点击 Resend 之后才抓 baseline。

### 5.3 尝试次数

即时流程最多执行 3 轮 OTP 挑战。出现 `PasswordAlreadySetError` 时直接记录
`already_set`，不进入后台。其他可恢复错误在即时轮次耗尽后进入后台交接。

## 6. 搜索与 settle 两阶段截止时间

GenericAPI 取码由单一截止时间改成两阶段状态机：

### 6.1 搜索阶段

- `search_deadline = start + OTP_MAX_WAIT`；
- 在此期限内只寻找满足本轮 baseline 与 `triggered_at` 的新候选；
- 没有候选到期时抛出取码超时。

### 6.2 确认阶段

- 候选在 `search_deadline` 前出现后，设置
  `settle_until = candidate_seen_at + OTP_SETTLE_SECONDS`；
- 即使 `settle_until` 晚于 `search_deadline`，仍继续轮询到 settle 完成；
- 候选在确认期发生变化时，用更新候选替换旧候选并重新开始 settle；
- 从首个候选开始的确认总时长设置硬上限
  `max(15 秒, 3 * OTP_SETTLE_SECONDS)`；
- 到硬上限仍持续变化时以“候选不稳定”失败，不返回未经确认的值。

轮询休眠时间必须取 `poll_interval` 和剩余阶段时间的较小值，避免一次 sleep 跨过
settle 边界。

建议默认配置：

```text
OTP_MAX_WAIT=120
OTP_SETTLE_SECONDS=5
OTP_POLL_INTERVAL=3
ROXY_PASSWORD_SETUP_MAX_RETRIES=3
```

这能使任务 1050 中 16:15:32 出现的候选继续确认至约 16:15:37，而不是在 16:15:35
因搜索总超时退出。

## 7. 注册结果与后台交接

### 7.1 即时成功

- 保存 `registration_password`；
- 设置 `password_setup_status=success`；
- 如可读取新 session，则优先保存刷新后的 token。

### 7.2 即时失败

即时设置密码异常不得改变注册成功结论。注册函数执行：

1. 保存账号、access token 和错误摘要；
2. 返回 `success=true`、`password_setup_handoff=true` 和 `account_id`；
3. 不在注册函数内部直接启动后台任务。

`run_roxy_registration` 的 `finally` 会先退出 driver 并清理临时 profile。调用方只有在函数
完整返回后才能看到 handoff 标记，因此由 `registration_service` 在返回后调用
`enqueue_account_password_setup(account_id, trigger="registration_handoff")`。这能保证后台
任务不会与注册清理逻辑同时操作同一个 Roxy profile。

如果后台入队成功，账号状态为 `queued`；入队失败则为 `failed`，但注册任务仍保持成功。
延迟重试期间同样使用 `queued`，并通过 `password_setup_next_retry_at` 区分“等待退避时间”
与“已经进入执行器队列”。

后台任务不需要沿用即时失败时临时生成的随机密码。它在入队时生成一次新的目标密码，
同一个后台任务的后续重试始终使用该密码，并且只在最终成功后保存为
`registration_password`。密码不得写入普通日志或错误文本。

## 8. 后台任务行为

后台服务复用现有 `password_setup_task_service`：

1. 从 DB 领取 `queued` 账号并标记 `running`；
2. 等待注册环境已经清理完成；
3. 尝试打开账号保存的 Roxy profile；
4. profile 已失效时，使用现有恢复逻辑创建新环境；
5. 调用同一 `_run_roxy_password_setup`，因此后台和即时流程共享 OTP 隔离规则；
6. 成功后回写密码、完成时间及 `success`；
7. `PasswordAlreadySetError` 回写 `already_set`；
8. 可恢复错误按退避策略重新排到队尾；不可恢复错误直接标记 `failed`。

重试延迟序列为：

- 第一次失败后 15 秒；
- 第二次失败后 60 秒；
- 第三次失败后 180 秒；
- 后续失败继续使用 180 秒，但不能超过总尝试次数。

为保持现有配置兼容，`ROXY_PASSWORD_SETUP_MAX_RETRIES` 继续沿用当前实现语义：它表示
后台任务的最大总尝试次数，而不是“首次之外的追加次数”。默认值 3 表示最多执行 3 次，
因此默认实际使用 15 秒和 60 秒两档退避；用户将其设置为 4 时才会使用第三档 180 秒。

延迟调度不占用密码设置 worker。服务在延迟到期后才重新占用队列槽并提交任务。
服务重启时，现有恢复逻辑把中断的 queued/running 任务标为 failed，账号不会丢失，用户
可从账号页重新入队。

## 9. 状态机与界面语义

设置密码状态：

```text
未启用 -> null
即时执行成功 -> success
检测到已有密码 -> already_set
即时失败并完成后台交接 -> queued
后台 worker 领取 -> running
可恢复失败且还有次数 -> queued
后台成功 -> success
不可恢复或次数耗尽 -> failed
```

注册任务显示“成功”时，账号页应分别展示设置密码状态：

- `queued`：注册成功，等待后台设置密码；
- `running`：正在设置密码；
- `success`：密码设置成功；
- `already_set`：账号已经拥有密码；
- `failed`：注册成功，但设置密码最终失败，可手动重试。

注册任务日志不得把后台排队描述为注册失败。

## 10. 日志与诊断

新增或调整日志应包含：

- `purpose=registration|password_setup`；
- 挑战轮次和触发时间；
- baseline 中消息 ID 数量；
- 候选消息 ID、消息时间、是否命中 baseline；
- 当前处于 search 或 settle 阶段；
- 后台 handoff、队列状态、重试次数和下次重试时间；
- Roxy profile 是复用、失效恢复还是新建。

验证码值和目标密码不应新增到普通 INFO 日志。现有直接打印 OTP 的日志可在实施时改为
脱敏标识，但不作为本次功能成功的前置条件。

## 11. 测试设计

### 11.1 GenericAPI 单元测试

- baseline 中代码为 `119006`，新消息 ID 也返回 `119006`：接受。
- baseline 和当前消息 ID、时间、代码均未变化：拒绝并继续等待。
- 消息 ID缺失，但新时间戳晚于触发时间且代码相同：接受。
- 候选在搜索截止前最后一秒出现：获得完整 settle 窗口并返回。
- 候选从未稳定直到确认硬上限：失败，不返回未确认值。

### 11.2 设置密码流程测试

- authorize 自动触发邮件时，首次挑战不点击 Resend。
- 首次等待超时后，顺序必须是 capture baseline、记录时间、点击 Resend、等待 OTP。
- 页面拒绝 OTP 后，新一轮使用新的 baseline 和触发时间。
- GenericAPI 新邮件复用注册验证码时仍提交该验证码。
- 页面已经自动进入新密码页时停止 OTP 重试。

### 11.3 注册与后台交接测试

- 即时设置密码失败时，注册账号仍保存且任务返回成功。
- `run_roxy_registration` 完成 profile 清理后，`registration_service` 才调用后台入队。
- 入队成功后账号状态为 queued；入队失败为 failed，但注册状态不改变。
- 后台打开已删除 profile 时创建新环境并完成设置密码。
- 后台可恢复错误按 15/60/180 秒序列重新排到队尾且不占 worker；默认最多 3 次总尝试，
  因而使用前两档延迟。
- 后台成功时密码和 success 状态正确回写。
- 达到重试上限或服务重启恢复时账号仍存在，错误可见且可以手动重试。

### 11.4 回归验证

- 运行 GenericAPI、邮件 provider、Roxy 注册 OTP、Roxy 设置密码、后台队列相关测试；
- 运行全量 pytest；
- 使用模拟时间复现任务 1050 的“最后几秒发现候选”路径；
- 实际运行一条启用注册后设密的 GenericAPI 账号，确认没有立即重复 Resend，并观察
  即时成功或后台 handoff 的完整状态变化。

## 12. 实施边界与检查点

实施应分成以下独立检查点，每批测试通过后再继续：

1. GenericAPI 两阶段截止时间；
2. 设置密码独立 baseline 和取消首次立即 Resend；
3. 注册成功后的后台 handoff 与状态更新；
4. 延迟重试与可观测性；
5. 相关测试、全量回归和项目重启验证。

每个检查点只修改与该阶段直接相关的文件，不覆盖工作区现有的其他未提交改动。
