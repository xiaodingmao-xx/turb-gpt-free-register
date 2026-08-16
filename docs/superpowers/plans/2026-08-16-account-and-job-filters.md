# 账号与注册任务筛选 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** 为账号页和注册任务页增加准确的后端筛选，并将账号批量操作栏调整为居中悬浮、支持清空选择。

**Architecture:** 账号页沿用现有 `plan` 查询参数，新增 `plus_trial_eligible` 过滤值并由数据库分页查询执行；注册任务页给 `/api/jobs` 增加 `status` 查询参数，在服务端过滤后再分页和统计。前端保留当前原生 JavaScript 架构，仅新增筛选控件、状态变量和悬浮栏事件，不改变任务或账号数据模型。

**Tech Stack:** Flask、Python 文件数据库、单文件 HTML/CSS/原生 JavaScript、unittest/pytest。

## Global Constraints

- 所有新增代码注释使用中文。
- 账号筛选必须在后端分页前执行，注册任务状态统计必须对应当前筛选结果。
- “取消选择”表示清空全部已选账号并隐藏账号悬浮操作栏。
- 不删除现有数据文件，不引入新的第三方依赖。

---

### Task 1: 后端筛选能力

**Files:**
- Modify: `core/db.py`（账号套餐过滤与任务列表查询）
- Modify: `webui/app.py`（账号与任务接口参数）
- Test: `tests/test_account_list_query.py`
- Test: `tests/test_webui_account_features.py`
- Test: `tests/test_webui_jobs.py`（若已有任务接口测试则扩展该文件）

**Interfaces:**
- `db._account_matches_plan_filter(row, "plus_trial_eligible")` 只匹配 `plus_trial_eligible` 为真。
- `GET /api/jobs` 在服务端完成展示状态过滤后再分页。
- `GET /api/jobs?status=available|failed` 在过滤后返回 `items`、`total` 和对应的 `status_counts`。

- [x] **Step 1: Write failing tests**：覆盖可试用账号只返回资格为真的记录；覆盖 `/api/jobs?status=failed` 只返回 `failed` 任务且总数正确；覆盖未知状态值按全部处理。
- [x] **Step 2: Run the focused tests**：运行 `python -m pytest tests/test_account_list_query.py tests/test_webui_account_features.py tests/test_webui_jobs.py -q`，确认新断言因参数尚未实现而失败。
- [x] **Step 3: Implement minimal backend changes**：添加 `plus_trial_eligible` 分支；为任务查询增加状态白名单和状态过滤；让 `/api/jobs` 将筛选后的任务用于分页与计数。
- [x] **Step 4: Run focused tests again**：确认新增测试和原有相关测试通过。

### Task 2: 前端筛选控件与查询状态

**Files:**
- Modify: `webui/templates/index.html`（注册任务工具栏、账号筛选栏及相关 JavaScript）
- Test: `tests/test_webui_account_features.py` 或新增模板静态断言测试

**Interfaces:**
- 账号页新增 `showTrialEligibleAccountsV2` 按钮，使用 `SHOW_TRIAL_ELIGIBLE_ACCOUNTS` 生成 `plan=plus_trial_eligible`。
- 注册页新增 `jobStatusFilterV2` 下拉框，使用 `JOB_STATUS_FILTER` 生成 `/api/jobs?status=...`。

- [x] **Step 1: Write failing template/API assertions**：断言模板包含“可Plus试用”和“可用/失败”入口，并断言接口参数能被转发。
- [x] **Step 2: Run focused tests and observe failure**。
- [x] **Step 3: Add controls and event handlers**：筛选变更时重置页码、清理对应选择集、刷新列表；筛选值保留在当前页面生命周期内。
- [x] **Step 4: Run focused tests and a template syntax check**：确认接口与模板均通过。

### Task 3: 居中悬浮操作栏与取消选择

**Files:**
- Modify: `webui/templates/index.html`
- Test: `tests/test_webui_account_features.py`（模板静态断言）

**Interfaces:**
- 新增 `clearSelectedAccounts()` 清空 `ACCOUNT_SELECTED`，调用 `renderAccounts()` 更新勾选框与悬浮栏。
- `btnFloatClearSelectedV2` 绑定到清空操作。

- [x] **Step 1: Write failing template assertions**：断言存在取消选择按钮、清空函数和居中布局关键样式。
- [x] **Step 2: Run the focused test and confirm failure**。
- [x] **Step 3: Implement CSS and event binding**：悬浮栏使用 `left:50%` 加 `transform:translateX(-50%)` 固定在底部正中，移动端改为左右留边；提高层级，复制提示不遮挡操作栏；取消选择后隐藏悬浮栏。
- [x] **Step 4: Run focused tests and manual browser verification**：验证复制行、滚动、取消选择和再次勾选。

### Task 4: 全量验证与交付

**Files:**
- Modify: `docs/superpowers/plans/2026-08-16-account-and-job-filters.md`（勾选完成项）

- [x] **Step 1: Run the full test suite**：`python -m pytest -q` 已通过 200 项。
- [x] **Step 2: Restart the local WebUI**：已重启当前项目的 5000 端口进程。
- [x] **Step 3: Verify HTTP response and key UI text**：`http://127.0.0.1:5000/login` 返回 200，模板测试和 JavaScript 语法检查通过。
- [x] **Step 4: Commit with a Chinese message and push**：已使用中文提交信息提交本次修改文件，并推送到远程 `main`；已有未跟踪计划文件未纳入提交。
