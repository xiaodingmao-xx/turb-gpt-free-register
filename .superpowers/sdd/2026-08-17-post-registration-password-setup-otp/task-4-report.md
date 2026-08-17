# Task 4：后台退避重试与密码目标生命周期

## 状态

已实现并通过指定测试。退避重试使用 15/60/180 秒 daemon `Timer`；创建 Timer 不占 `_QUEUE_SLOTS`，回调非阻塞取槽，队满时保持 attempt 并在 5 秒后重排。首次 enqueue 解析出的密码沿所有重试复用。

## RED

基线首先误用了系统 Python，收集阶段因 Python 版本过旧及缺少 `pyotp` 失败；随后切换到项目 `.venv`，原有目标测试基线为 `36 passed in 12.45s`。

新增行为测试后执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_password_setup_task_service.py tests/test_password_setup_concurrency.py tests/test_account_list_query.py -q
```

真实输出摘要：

```text
10 failed, 33 passed in 11.45s
```

失败均为预期缺失行为：`_retry_delay_seconds` 不存在；重试未创建 Timer 而是立即占槽提交；队满未按 5 秒重排；`password_setup_next_retry_at` 未持久化/清理；`queue_settings` 未区分 delayed；恢复流程未清理 next retry 时间。

## GREEN

实现后执行同一指定命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_password_setup_task_service.py tests/test_password_setup_concurrency.py tests/test_account_list_query.py -q
```

真实输出：

```text
...........................................                              [100%]
43 passed in 15.84s
```

额外验证：

```powershell
git diff --check
.\.venv\Scripts\python.exe -m py_compile core/password_setup_task_service.py core/db.py tests/test_password_setup_task_service.py tests/test_password_setup_concurrency.py tests/test_account_list_query.py
```

结果：两条命令退出码均为 0；`git diff --check` 仅报告工作树现有 LF→CRLF 提示，无空白错误。

## 变更文件

- `core/password_setup_task_service.py`
  - 新增 `_retry_delay_seconds(attempt)`，实现 15/60/180 秒上限退避。
  - `_schedule_password_setup_retry` 先持久化 delayed 状态，再启动 daemon Timer。
  - Timer 回调非阻塞获取槽；队满保持 next attempt，更新 5 秒 next retry 并重新 arm。
  - 成功取槽后清除 next retry，再提交原 `_run_task_wrapper`；提交失败释放槽并标记失败。
  - 新增日志只记录 attempt/max_attempts/delay/next_retry_at，并再次脱敏错误文本。
  - `queue_settings` 新增 delayed 计数并从 waiting/positions 排除未来重试。
- `core/db.py`
  - `requeue_account_password_setup` 新增 `next_retry_at` 关键字参数并持久化。
  - claim、mark-running、update、recover 均清理 `password_setup_next_retry_at`。
  - 失败结果即使携带 password 也不会保存，成功保存逻辑保持原样。
- `tests/test_password_setup_task_service.py`
  - 覆盖退避序列、daemon Timer 不占槽、队满 5 秒重排且 attempt 不变、密码只解析一次并跨重试复用、提交失败释放槽、DB 生命周期、delayed 队列统计。
- `tests/test_password_setup_concurrency.py`
  - 覆盖 delayed Timer 创建不增加 active worker。
- `tests/test_account_list_query.py`
  - 覆盖服务恢复时清理 delayed 时间。

## 自审

- 最大总尝试次数仍由 `_max_password_setup_attempts()` 控制，默认 3；attempt 从 1 开始，attempt=3 后不再调度。
- `resolve_password_setup_request` 仍只在 enqueue 调用一次；runner、Timer 与重排均传递闭包中的同一 resolved password。
- 未修改 `config/roxybrowser.py`、GenericAPI、注册交接或 WebUI。
- 未修改 profile 恢复及每次后端执行的新 OTP baseline 流程。
- 新增生产日志不包含目标密码或 OTP 明文；错误在调度前及写日志前脱敏。
- `core/db.py` 中其他任务已有的邮箱恢复 hunk 原样保留。

## 风险与关注点

- daemon Timer 是进程内状态，进程退出后不会恢复；现有 `recover_interrupted_password_setups()` 会把遗留 queued/running 任务标记失败，需手动重新提交。这与当前密码只保存在进程内、不能在重启后自动恢复的安全模型一致。
- 共享工作区包含大量其他任务未提交修改，尤其 `core/db.py` 是混合文件；提交时必须只分离本任务 hunk，否则保持未提交。
