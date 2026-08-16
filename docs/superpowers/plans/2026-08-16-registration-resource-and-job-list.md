# 注册资源保护与任务列表性能优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 防止 Roxy 资源错误导致同一邮箱被循环领取，并将注册任务列表接口从逐条重复扫描改为分页、批量计算，同时向前端展示可操作的资源状态。

**Architecture:** 邮箱池保留 `available/used/failed/disabled` 状态，并增加短期 `cooldown_until` 元数据；Roxy 错误由注册服务分类，连续资源错误触发批次熔断，邮箱仅冷却而不判废。任务列表接口先筛选/分页，再使用一次性加载的任务和账号索引补充重试信息；前端继续轮询，但避免请求重叠并显示资源暂停提示。

**Tech Stack:** Python 3、unittest、Flask、现有 JSON 数据库、原生 JavaScript。

## Global Constraints

- 不批量删除文件或目录，不修改用户现有邮箱和任务数据。
- 不把 Roxy 窗口/内存错误标记为邮箱失效。
- 继续兼容现有 `outlook`、`generic_api` 邮箱来源和旧任务 JSON 字段。
- 所有生产代码变更先有会失败的回归测试，再实现最小修改。

---

### Task 1: 为通用 API 邮箱增加冷却与轮询领取

**Files:**
- Modify: `core/db.py:2186-2231`
- Modify: `core/email_provider.py:212-235`
- Create: `tests/test_generic_api_pool_selection.py`

**Interfaces:**
- `claim_next_generic_api_email()` 继续返回邮箱对象或 `None`，但跳过 `cooldown_until` 尚未到期的记录。
- `release_unconsumed_generic_api_email(email, note=None, cooldown_seconds=0)` 在资源错误时保留邮箱为 `available`，并写入冷却结束时间。

- [ ] **Step 1: 写失败测试**

在 `tests/test_generic_api_pool_selection.py` 中覆盖：按 ID 排序时第一条邮箱处于冷却，领取第二条可用邮箱；释放邮箱时写入冷却时间；冷却过期后邮箱重新可领取。

- [ ] **Step 2: 运行测试确认失败**

运行：`python -m pytest tests/test_generic_api_pool_selection.py -q`

预期：因领取函数不识别 `cooldown_until` 或释放函数不接受冷却参数而失败。

- [ ] **Step 3: 实现最小逻辑**

在数据库锁内使用当前时间过滤冷却记录；释放时仅在 `status == "used"` 且没有本地账号时更新状态。冷却时间使用 ISO 字符串，兼容缺失字段和非法字段。

- [ ] **Step 4: 运行测试确认通过**

运行：`python -m pytest tests/test_generic_api_pool_selection.py -q`

- [ ] **Step 5: 运行现有邮箱相关测试**

运行：`python -m pytest tests/test_generic_api_yangyang.py tests/test_email_provider_gptmail.py -q`

---

### Task 2: 分类 Roxy 资源错误并增加注册批次熔断

**Files:**
- Modify: `core/registration_service.py:132-210,390-455`
- Modify: `core/roxybrowser_client.py:520-590`
- Create: `tests/test_registration_resource_guard.py`

**Interfaces:**
- 新增 `classify_registration_error(error) -> str`，返回 `resource`, `mailbox`, `registration` 或 `unknown`。
- 新增 `get_resource_guard_status() -> dict`，返回当前连续资源错误次数、是否暂停和暂停原因。
- 资源错误回收邮箱时调用 `release_email_if_unconsumed(..., cooldown_seconds=600)`；验证码 404/超时不走资源冷却。

- [ ] **Step 1: 写失败测试**

覆盖以下行为：`窗口额度不足` 和 `内存使用率超过` 被分类为 `resource`；验证码 404/通用 API 超时被分类为 `mailbox`；连续三次资源错误后返回暂停状态；成功任务会清零连续错误。

- [ ] **Step 2: 运行测试确认失败**

运行：`python -m pytest tests/test_registration_resource_guard.py -q`

预期：因分类器和熔断状态尚不存在而失败。

- [ ] **Step 3: 实现最小逻辑**

在注册服务中增加进程内线程安全计数器；任务异常处理先分类，再决定邮箱回收策略。资源错误达到阈值时阻止后续新任务领取邮箱并返回明确错误；不修改已有账号和邮箱的永久状态。

- [ ] **Step 4: 运行测试确认通过**

运行：`python -m pytest tests/test_registration_resource_guard.py -q`

- [ ] **Step 5: 运行注册服务和 Roxy 相关测试**

运行：`python -m pytest tests/test_registration_network.py tests/test_roxy_saved_proxy.py tests/test_roxy_password_setup.py -q`

---

### Task 3: 优化注册任务列表分页和批量重试信息

**Files:**
- Modify: `core/registration_service.py:506-562`
- Modify: `webui/app.py:205-245,2379-2405`
- Create: `tests/test_webui_jobs_performance.py`

**Interfaces:**
- 新增 `decorate_retry_info(rows) -> list[dict]`，在一次加载的任务/账号索引上计算当前列表需要的重试字段。
- `/api/jobs` 保持现有参数和返回字段兼容；分页响应继续包含 `items`、`status_counts`、`all_status_counts`。

- [ ] **Step 1: 写失败测试**

测试分页请求只把当前页任务传给重试信息装饰器；测试当前页没有失败任务时不触发全量账号逐条查询；测试现有状态筛选响应字段不变。

- [ ] **Step 2: 运行测试确认失败**

运行：`python -m pytest tests/test_webui_jobs.py tests/test_webui_jobs_performance.py -q`

预期：性能测试因 `/api/jobs` 目前先全量装饰而失败。

- [ ] **Step 3: 实现最小逻辑**

后端先读取任务并计算原始状态统计；对状态筛选后分页；仅对当前页调用批量重试信息函数。保留任务详情接口的完整错误日志，不改变列表摘要格式。

- [ ] **Step 4: 运行测试确认通过**

运行：`python -m pytest tests/test_webui_jobs.py tests/test_webui_jobs_performance.py -q`

- [ ] **Step 5: 运行全部 WebUI 测试**

运行：`python -m pytest tests/test_webui_*.py -q`

---

### Task 4: 前端显示资源暂停状态并避免刷新重叠

**Files:**
- Modify: `webui/templates/index.html:job list rendering and refreshJobs timer`
- Modify: `webui/templates/index_legacy.html:matching fallback job list rendering`
- Create: `tests/test_webui_resource_status.py`

**Interfaces:**
- `/api/jobs` 可返回 `resource_guard` 对象；旧接口缺失时前端按未暂停处理。
- 页面显示“Roxy 资源不足，任务已暂停”及继续刷新按钮，不自动重启注册任务。

- [ ] **Step 1: 写失败测试**

检查模板包含资源状态容器、暂停文案、刷新请求中的并发保护变量，并保留现有任务状态筛选控件。

- [ ] **Step 2: 运行测试确认失败**

运行：`python -m pytest tests/test_webui_resource_status.py -q`

- [ ] **Step 3: 实现最小逻辑**

在 `refreshJobs()` 中增加请求进行中标记和 finally 清理；渲染响应中的 `resource_guard`；将轮询间隔调整为 5 秒，避免 3 秒轮询覆盖 5 秒接口处理。

- [ ] **Step 4: 运行测试确认通过**

运行：`python -m pytest tests/test_webui_resource_status.py tests/test_webui_jobs.py -q`

---

### Task 5: 集成验证、重启本地项目并提交

**Files:**
- Modify: 仅提交前述任务涉及的代码和测试文件。
- Preserve: `docs/superpowers/plans/2026-08-15-account-page-registration-metadata.md` 不纳入本次提交。

- [ ] **Step 1: 运行完整测试集**

运行：`python -m pytest -q`

预期：退出码为 0，且无新增失败。

- [ ] **Step 2: 做只读回归检查**

确认 `EMAIL_SOURCE` 仍为当前用户配置；确认邮箱 JSON 的数量和状态未被测试改动；请求 `/login` 和 `/api/jobs?paged=1&page=1&page_size=20`，确认返回 200。

- [ ] **Step 3: 重启本地项目**

先停止当前项目进程，再用项目既有启动方式启动；不触碰 Roxy 外部窗口和邮箱数据。

- [ ] **Step 4: 查看 Git 差异并提交**

运行 `git diff --check` 和 `git status --short`，只提交本次相关文件，提交信息为：`fix: protect registration resources and speed up job list`。

