# 账号页设置密码、筛选与排序 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** 在账号页面增加安全的设置密码任务入口，并让邮箱、来源可筛选，支持跨分页的服务端排序。

**Architecture:** 账号列表继续由 `core.db` 在内存加载 JSON 后过滤、排序、分页；`webui/app.py` 透传并校验 `email/source/sort/order` 参数。设置密码单独使用后台线程池提交任务，任务内部打开账号记录的 Roxy profile，复用 `core.roxy_registration` 的同源 CSRF/OTP/密码流程，并通过已有 `PasswordSetupGate` 限制实际并发。前端只提交任务和轮询账号状态，不返回明文密码。

**Tech Stack:** Python 3、Flask、JSON 文件存储、ThreadPoolExecutor、原生 HTML/JavaScript、unittest。

## Global Constraints

- 不删除或批量覆盖用户文件；代码修改使用 `apply_patch`。
- 敏感字段只按需通过 `/api/accounts/<id>/secret` 读取，设置密码接口和任务状态不回显明文密码。
- 默认 `ROXY_PASSWORD_SETUP_WORKERS=1`，保留 Roxy `/browser/create` 已有串行锁。
- 历史账号必须有保存的 Roxy `profile_id`；不存在时任务明确失败并提示缺少环境。
- 所有新增行为先写失败测试，再写最小生产实现。

---

### Task 1: 账号列表过滤与服务端排序

**Files:**
- Modify: `core/db.py`
- Modify: `webui/app.py`
- Modify: `webui/templates/index.html`
- Test: `tests/test_account_list_query.py`

**Interfaces:**
- `db.list_accounts_page(..., email_filter=None, source_filter=None, sort_key="id", sort_order="desc") -> dict`
- `GET /api/accounts?...&email=...&source=...&sort=...&order=...`
- 允许排序键：`id,email,email_source,plan,totp,codex,agent_token,created_at`；非法键回退 `id`，非法 order 回退 `desc`。

- [x] **Step 1: Write failing tests** for email/source filtering, created-time ascending order, and rejecting unsafe sort keys.
- [x] **Step 2: Run** the focused test with the project `.venv` and confirm the missing arguments fail before implementation.
- [x] **Step 3: Implement** filtering before pagination and stable sort after filtering with `id` as a tie-breaker; expose only compact account rows.
- [x] **Step 4: Add** explicit email input, source select, sortable table headers, sort arrows, and reset-to-page-1 behavior in the account page.
- [x] **Step 5: Run** the focused tests and verify the existing plan-status polling uses the same query parameters.

### Task 2: 设置密码后台任务服务

**Files:**
- Create: `core/password_setup_task_service.py`
- Modify: `core/db.py`
- Modify: `core/roxy_registration.py`
- Test: `tests/test_password_setup_task_service.py`

**Interfaces:**
- `enqueue_account_password_setup(account_id, email, mode, password) -> dict`
- `queue_settings() -> dict`
- Account fields: `password_setup_status` (`queued/running/success/failed`), timestamps, mode, and redacted error.

- [x] **Step 1: Write failing tests** for queue acceptance, duplicate-account rejection, missing profile failure, success persistence, and password redaction.
- [x] **Step 2: Run** the focused test with the project `.venv` and confirm the service is initially missing.
- [x] **Step 3: Implement** atomic DB claim/start/finish helpers and a bounded executor; store the selected mode and only a boolean password-set result in task responses.
- [x] **Step 4: Implement** the Roxy runner using the account’s saved `extra_json.roxybrowser.profile_id`, `RoxyBrowserClient.open_profile(profile_id, allow_existing_profile=True)`, `_build_driver`, and `_run_roxy_password_setup`; close an existing profile but do not delete it.
- [x] **Step 5: Run** focused task tests, then verify all existing password concurrency tests remain green.

### Task 3: Flask API and account-page controls

**Files:**
- Modify: `webui/app.py`
- Modify: `webui/templates/index.html`
- Test: `tests/test_webui_account_features.py`

**Interfaces:**
- `POST /api/accounts/<int:acc_id>/password-setup` body `{mode, password}`; returns `202` when queued, `409` for duplicate, `404` for missing account, and `400` for invalid input.
- `POST /api/accounts/password-setup-bulk` body `{account_ids, mode, password}`; returns per-account queued/skipped results.
- `GET /api/accounts/password-setup-status` returns queue settings and compact status rows for selected/current-page accounts.

- [x] **Step 1: Write failing API tests** for single-account and bulk queue submission, invalid mode, and no plaintext password in JSON.
- [x] **Step 2: Run** the focused test with the project `.venv` and confirm the routes are initially missing.
- [x] **Step 3: Register** API routes and validate password length/mode without logging the password.
- [x] **Step 4: Add** row-level “设置密码” and selected-row bulk button, modal with add/reset choice and show/hide password, and status rendering.
- [x] **Step 5: Run** focused web tests and inspect the account page markup for the existing 13-column alignment.

### Task 4: Regression verification and restart

**Files:**
- Modify: `docs/superpowers/specs/2026-08-12-password-setup-task-design.md` if the final endpoint names differ.

- [x] **Step 1:** Run `python -m unittest discover -s tests -p 'test_*.py'` with `.venv\Scripts\python.exe` (109 tests passed).
- [x] **Step 2:** Run `python -m py_compile core/db.py core/password_setup_task_service.py core/roxy_registration.py webui/app.py`.
- [x] **Step 3:** Restart the local project with `.venv\Scripts\python.exe`; the listener is PID 37008 on port 5001.
- [x] **Step 4:** Verify the authorized home page and account API return HTTP 200; verify new account-page element IDs are present.
