# 设置密码失败清理与队列末尾重试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with checkpoints.

**Goal:** 设置密码任务在临时 Roxy 环境执行失败时默认关闭并删除该临时环境，并在允许重试且未超过次数时把任务重新放到队列末尾。

**Architecture:** 保留现有 `ThreadPoolExecutor` 和队列槽位机制，由任务执行函数只负责执行、清理和返回结果，由 wrapper 在释放当前执行槽位后负责重新入队。使用 `RoxyOpenResult.created_by_run` 区分本次新建环境和账号原有环境，绝不删除原有环境。重试次数写入账号 JSON，密码只在当前进程内传递，不落盘。

**Tech Stack:** Python 3、现有 JSON 存储、`core.password_setup_task_service`、`core.roxybrowser_client`、Flask WebUI、pytest。

## Global Constraints

- 仅删除本次设置密码任务新建的 Roxy profile；原有 profile 失败时只关闭，不删除。
- 默认最多 3 次尝试；达到上限后标记最终失败，不无限重试。
- `PasswordAlreadySetError` 视为成功，不重试。
- 不在日志、数据库或接口响应中保存明文密码。
- 保留当前工作区已有的 OTP 修复和其他未提交改动，不覆盖无关修改。

---

### Task 1: 增加重试配置和账号状态字段

**Files:**
- Modify: `config/roxybrowser.py`
- Modify: `core/db.py`
- Test: `tests/test_config_defaults.py`
- Test: `tests/test_password_setup_task_service.py`

**Interfaces:**
- Produces `password_setup_attempt`、`password_setup_max_attempts`、`password_setup_last_error` 三个可选账号字段。
- Produces `db.requeue_account_password_setup(acc_id, error, attempt, max_attempts) -> bool`。

- [ ] **Step 1: Write failing tests**

```python
def test_password_setup_requeue_moves_status_to_queued(monkeypatch):
    row = {"password_setup_status": "running", "password_setup_attempt": 1}
    assert requeue_account_password_setup(7, "timeout", attempt=2, max_attempts=3) is True
    assert row["password_setup_status"] == "queued"

def test_password_setup_retry_defaults_are_bounded():
    assert config.ROXY_PASSWORD_SETUP_MAX_RETRIES == 3
    assert config.ROXY_PASSWORD_SETUP_DELETE_TEMP_PROFILE_ON_FAILURE is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config_defaults.py tests/test_password_setup_task_service.py -q`

Expected: FAIL because the new configuration and database helper do not exist.

- [ ] **Step 3: Add configuration defaults**

在 `config/roxybrowser.py` 增加：

```python
ROXY_PASSWORD_SETUP_MAX_RETRIES = 3
ROXY_PASSWORD_SETUP_DELETE_TEMP_PROFILE_ON_FAILURE = True
```

这里的 3 表示总尝试次数，不是额外重试次数。

- [ ] **Step 4: Implement the database retry transition**

`requeue_account_password_setup` 在 `_LOCK` 内完成以下操作：

```python
row["password_setup_status"] = "queued"
row["password_setup_attempt"] = attempt
row["password_setup_max_attempts"] = max_attempts
row["password_setup_last_error"] = str(error or "")[:500]
row["password_setup_error"] = None
row["password_setup_queued_at"] = _now()
row["password_setup_started_at"] = None
row["password_setup_completed_at"] = None
row["updated_at"] = _now()
```

新的 `queued_at` 必须在重试时重新生成，队列排序自然会把它放到末尾。

- [ ] **Step 5: Initialize attempt metadata on first enqueue**

在 `claim_account_password_setup` 中将首次任务设置为 `attempt=1`，并保留 `max_attempts` 配置值。已有账号缺失这些字段时按首次入队兼容处理。

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_config_defaults.py tests/test_password_setup_task_service.py -q`

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add config/roxybrowser.py core/db.py tests/test_config_defaults.py tests/test_password_setup_task_service.py
git commit -m "feat: add password setup retry state"
```

### Task 2: 实现失败环境清理和错误分类

**Files:**
- Modify: `core/password_setup_task_service.py`
- Test: `tests/test_password_setup_task_service.py`

**Interfaces:**
- Produces `_is_retryable_password_setup_error(error) -> bool`。
- `_run_password_setup_task(...)` 返回包含 `ok`、`already_set`、`retryable`、`error`、`attempt` 的结果字典。

- [ ] **Step 1: Write failing cleanup tests**

```python
def test_failed_task_deletes_only_profile_created_by_current_run(fake_client, fake_driver):
    result = run_failed_task(opened_created_by_run=True, fake_client=fake_client, fake_driver=fake_driver)
    assert result["retryable"] is True
    assert fake_driver.events == ["quit"]
    assert fake_client.events == ["cleanup"]

def test_failed_task_does_not_delete_saved_profile(fake_client, fake_driver):
    run_failed_task(opened_created_by_run=False, fake_client=fake_client, fake_driver=fake_driver)
    assert fake_driver.events == ["quit"]
    assert fake_client.events == []

def test_password_already_set_is_not_retryable():
    assert _is_retryable_password_setup_error(PasswordAlreadySetError("already")) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_password_setup_task_service.py -q`

Expected: FAIL because the current `finally` only清理成功任务。

- [ ] **Step 3: Add explicit error classification**

永久错误直接返回不可重试；超时、Roxy API、页面加载、OTP、网络和 Selenium 临时异常返回可重试。`PasswordAlreadySetError` 单独转换为 `already_set=True`。

- [ ] **Step 4: Change failure cleanup order**

在 `_run_password_setup_task` 的 `finally` 中采用：

```python
if driver is not None:
    driver.quit()
if opened is not None:
    if succeeded or (
        opened.created_by_run
        and config.ROXY_PASSWORD_SETUP_DELETE_TEMP_PROFILE_ON_FAILURE
    ):
        client.cleanup_profile(opened)
```

失败时只删除 `created_by_run=True` 的临时 profile；原有 profile 不删除。清理异常写日志，但不能覆盖原始失败原因。

- [ ] **Step 5: Keep failure diagnostics without keeping the browser by default**

将日志改为记录 profile ID、是否新建、是否删除、原始异常和清理异常；默认不再保留失败窗口。成功任务仍使用现有清理策略。

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_password_setup_task_service.py -q`

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add core/password_setup_task_service.py tests/test_password_setup_task_service.py
git commit -m "fix: clean temporary Roxy profiles after password setup failure"
```

### Task 3: 在释放队列槽位后重新排队

**Files:**
- Modify: `core/password_setup_task_service.py`
- Modify: `core/db.py`
- Test: `tests/test_password_setup_concurrency.py`

**Interfaces:**
- Produces `_schedule_password_setup_retry(result, account_id, email, mode, password) -> bool`。
- Wrapper 保证先从 `_ACTIVE` 移除并释放 `_QUEUE_SLOTS`，再调用重试提交逻辑。

- [ ] **Step 1: Write failing ordering tests**

```python
def test_retry_is_submitted_after_current_slot_is_released():
    events = []
    run_task_wrapper_with_events(events, {"ok": False, "retryable": True, "attempt": 1, "max_attempts": 3})
    assert events == ["release", "retry"]

def test_retry_stops_at_max_attempts():
    assert _schedule_password_setup_retry(
        account_id=7, email="user@example.com", mode="post_login_add_password",
        password="secret", result={"ok": False, "retryable": True, "attempt": 3, "max_attempts": 3}
    ) is False

def test_requeued_task_gets_tail_timestamp():
    assert requeue_account_password_setup(7, "timeout", attempt=2, max_attempts=3) is True
    assert queue_settings()["positions"]["7"] > queue_settings()["positions"]["6"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_password_setup_concurrency.py -q`

Expected: FAIL because当前 wrapper 没有重试状态机。

- [ ] **Step 3: Implement retry scheduling**

重试判断逻辑：

```python
should_retry = (
    not result.get("ok")
    and not result.get("already_set")
    and result.get("retryable")
    and int(result.get("attempt", 1)) < int(result.get("max_attempts", 3))
)
```

wrapper 的顺序必须是：记录结果 → 移除 active → 释放 semaphore → `requeue_account_password_setup` → 用原来的进程内密码重新提交。

- [ ] **Step 4: Persist final failure only after retry exhaustion**

未达上限时保持 `queued`，将上次错误写入 `password_setup_last_error`；达到上限时写 `failed` 和 `password_setup_error`，并写明“已达到最大重试次数”。

- [ ] **Step 5: Add interrupted-task recovery**

服务启动时将没有进程内密码可继续执行的 `queued/running` 设置密码任务标记为失败，并提示“服务重启导致任务中断，请手动重新提交”，避免重启后出现永远排队的假任务。

- [ ] **Step 6: Run focused and full tests**

Run: `pytest tests/test_password_setup_concurrency.py tests/test_password_setup_task_service.py -q`

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add core/password_setup_task_service.py core/db.py tests/test_password_setup_concurrency.py
git commit -m "feat: retry failed password setup tasks at queue tail"
```

### Task 4: 暴露重试状态到 WebUI

**Files:**
- Modify: `webui/app.py`
- Modify: `webui/templates/index.html`
- Test: `tests/test_webui_account_features.py`

**Interfaces:**
- Account list response includes retry attempt and last error fields。
- UI displays `设置密码排队中（第 N/3 次）`、`失败后已重新排队`、`最终失败`。

- [ ] **Step 1: Write failing response/UI tests**

```python
def test_account_list_exposes_password_setup_retry_fields(client):
    response = client.get("/api/accounts?paged=1")
    item = response.get_json()["items"][0]
    assert "password_setup_attempt" in item
    assert "password_setup_max_attempts" in item
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_webui_account_features.py -q`

Expected: FAIL because compact account output does not expose the new fields。

- [ ] **Step 3: Add fields to compact account output**

在 `webui/app.py` 的 `_compact_account_for_list` 中加入：

```python
"password_setup_attempt",
"password_setup_max_attempts",
"password_setup_last_error",
```

- [ ] **Step 4: Update status rendering**

在 `webui/templates/index.html` 根据状态和次数渲染队列、执行、自动重试和最终失败文案，并将 `password_setup_last_error` 放入 title 或“设置密码日志”入口，不直接展示敏感信息。

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_webui_account_features.py tests/test_password_setup_task_service.py tests/test_password_setup_concurrency.py -q`

Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add webui/app.py webui/templates/index.html tests/test_webui_account_features.py
git commit -m "feat: show password setup retry status"
```

## Verification Checklist

- 临时新 profile 失败后最终被关闭并删除。
- 原有 profile 失败后没有调用删除接口。
- 第 1、2 次可重试失败会重新进入队列末尾。
- 第 3 次失败不会无限循环。
- 密码已设置不会重新调用设置密码任务。
- 重启后不会留下拿不到密码的假排队任务。
- 现有全部测试通过：`pytest -q`。
