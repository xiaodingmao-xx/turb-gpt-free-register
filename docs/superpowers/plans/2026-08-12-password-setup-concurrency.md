# 密码设置并发队列 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** 将 Roxy 密码设置从单个全局串行阶段改为由 `ROXY_PASSWORD_SETUP_WORKERS` 控制的安全并发门控，允许多个独立 Roxy 窗口同时修改密码。

**Architecture:** 不把 Selenium driver 跨线程传递。每个注册线程继续创建并持有自己的 Roxy driver；密码设置进入一个共享的并发门控，获得许可后在原线程中执行，释放后由下一个任务进入。账号锁防止同一邮箱重复进入密码设置。

**Tech Stack:** Python `threading.Semaphore`、Selenium 现有 Roxy 驱动、`unittest`。

## Global Constraints

- 默认 `ROXY_PASSWORD_SETUP_WORKERS=1`，保持现有安全行为。
- 设置为 `2` 或更高时，最多同时执行对应数量的密码设置窗口。
- Roxy `/browser/create` 现有串行锁必须保留。
- 不跨线程传递 Selenium driver。
- 密码、OTP、CSRF 和 authorize URL 不写入日志。
- 不修改账号有效状态；密码设置失败只返回任务失败。

---

### Task 1: 并发配置和门控组件

**Files:**
- Create: `core/password_setup_service.py`
- Modify: `config/roxybrowser.py`
- Modify: `webui/config_editor.py`
- Modify: `.env.example`
- Test: `tests/test_password_setup_concurrency.py`

**Interfaces:**
- `core.password_setup_service.run_password_setup(key: str, runner: Callable[[], T]) -> T`
- `core.password_setup_service.queue_settings() -> dict`
- `ROXY_PASSWORD_SETUP_WORKERS`，范围 `1..16`，默认 `1`
- `ROXY_PASSWORD_SETUP_QUEUE_LIMIT`，默认 `100`

- [x] **Step 1: Write the failing tests**

```python
def test_workers_two_allows_two_password_runners_at_once():
    gate = PasswordSetupGate(workers=2, queue_limit=10)
    ...
    self.assertEqual(max_active, 2)

def test_same_account_cannot_enter_twice():
    gate = PasswordSetupGate(workers=2, queue_limit=10)
    self.assertFalse(gate.try_claim_account("same@example.com"))
```

- [x] **Step 2: Run the focused test and verify it fails**

Run:

```powershell
$py = '.venv\Scripts\python.exe'
& $py -m unittest -v tests.test_password_setup_concurrency
```

Expected: import failure because `core.password_setup_service` does not exist.

- [x] **Step 3: Implement the gate**

Implement a `PasswordSetupGate` with:

```python
class PasswordSetupGate:
    def __init__(self, workers: int = 1, queue_limit: int = 100): ...
    def run(self, key: str, runner: Callable[[], T]) -> T: ...
```

`run()` must acquire the worker semaphore, atomically claim the normalized account key, execute `runner()` in the caller thread, then release both resources in `finally`. Queue saturation raises a clear `RuntimeError` instead of silently dropping a task. Configuration values are clamped to workers `1..16` and queue limit `workers..5000`.

- [x] **Step 4: Run focused tests and verify they pass**

```powershell
& $py -m unittest -v tests.test_password_setup_concurrency
```

- [x] **Step 5: Add configuration and WebUI fields**

Add `ROXY_PASSWORD_SETUP_WORKERS` and `ROXY_PASSWORD_SETUP_QUEUE_LIMIT` to the Roxy config, env override mapping, `.env.example`, and the RoxyBrowser configuration group. Explain that `1` is serial and `2+` enables multiple independent windows.

- [x] **Step 6: Run focused tests again**

```powershell
& $py -m unittest -v tests.test_password_setup_concurrency
```

---

### Task 2: Integrate the gate into the Roxy registration password stage

**Files:**
- Modify: `core/roxy_registration.py`
- Test: `tests/test_password_setup_concurrency.py`

**Interfaces:**
- Existing `_run_roxy_password_setup(driver, email)` remains the actual Selenium operation.
- The call site wraps it with `run_password_setup(email, lambda: _run_roxy_password_setup(driver, email))`.

- [x] **Step 1: Add a failing integration test**

Patch the module configuration to `ROXY_PASSWORD_SETUP_WORKERS=2`, run two blocking runners through the public `run_password_setup` interface, and assert both can be active simultaneously while each runner remains in its caller thread.

- [x] **Step 2: Run the focused test and verify it fails**

```powershell
& $py -m unittest -v tests.test_password_setup_concurrency
```

- [x] **Step 3: Wrap the existing Roxy password call**

Keep driver creation and Selenium operations in the registration thread. Only use the gate around `_run_roxy_password_setup`. Log only email and concurrency counters, never password or OTP values.

- [x] **Step 4: Run the focused tests**

```powershell
& $py -m unittest -v tests.test_password_setup_concurrency tests.test_roxy_password_setup
```

---

### Task 3: Update documentation and verify the running project

**Files:**
- Modify: `docs/superpowers/specs/2026-08-12-password-setup-task-design.md`
- Modify: `README.md`

- [x] **Step 1: Update the design and README**

Replace the default serial-only wording with the configurable behavior, document `ROXY_PASSWORD_SETUP_WORKERS=1/2/3`, and state that Roxy creation remains serial while independent opened environments can execute concurrently.

- [x] **Step 2: Run the complete test suite and compile check**

```powershell
& $py -m unittest discover -s tests -p 'test_*.py'
& $py -m py_compile core\password_setup_service.py core\roxy_registration.py config\roxybrowser.py webui\config_editor.py
```

- [x] **Step 3: Restart only after confirming no active registration tasks**

Restart the project WebUI, verify `http://127.0.0.1:5001/` returns HTTP 200, and confirm the loaded worker configuration. Do not execute a real password modification during verification.
