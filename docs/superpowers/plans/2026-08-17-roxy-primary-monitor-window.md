# RoxyBrowser 主屏启动窗口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 RoxyBrowser 在创建/打开时直接使用第一块显示器的初始位置，消除窗口先在第二块屏幕出现、再被 Selenium 移回笔记本屏幕的闪现。

**Architecture:** 把窗口位置配置放到 Roxy Profile 的创建参数中，使用官方 `positionSwitch=true` 与 `windowRatioPosition="0,0"` 作为启动位置来源；不再依赖 `/browser/open` 的 Chrome 参数覆盖。API 返回进程 PID 后，在 Selenium 连接前用 Windows 原生窗口 API 将窗口预定位到主显示器，现有 Selenium 居中逻辑保留为兜底。

**Tech Stack:** Python 3、RoxyBrowser HTTP API、Selenium、Windows User32 ctypes、pytest/unittest。

## Global Constraints

- `/browser/open` 的 `args` 不得加入 `--window-position` 或 `--window-size`，Roxy 官方将这些参数列为系统内置且修改不生效。
- `windowRatioPosition="0,0"` 表示 Roxy 的第一个显示器左上角；实施前将笔记本屏幕设为 Windows 主显示器。
- 不新增第三方 Windows GUI 依赖，窗口定位只使用标准库 `ctypes`。
- 保留非 Windows 和 headless 模式的现有行为。
- 每个阶段必须先写测试、单独验证，再进入下一阶段；不得覆盖工作区中与本任务无关的已有修改。

---

### Task 1: 将主屏位置写入新 Profile 创建参数

**Files:**
- Modify: `config/roxybrowser.py:128-133`
- Modify: `tests/test_roxy_saved_proxy.py` 或新建 `tests/test_roxy_window_position.py`

**Interfaces:**
- Consumes: `RoxyBrowserClient.create_profile()` 读取的 `ROXY_PROFILE_CREATE_PAYLOAD`。
- Produces: 新建 Profile 请求中始终包含 `positionSwitch=True`、`windowRatioPosition="0,0"`，并允许用户在 `ROXY_PROFILE_CREATE_PAYLOAD` 中覆盖。

- [ ] **Step 1: Write the failing test**

```python
def test_create_profile_requests_first_display_position():
    client = RoxyBrowserClient()
    calls = []

    def request(method, path, **kwargs):
        calls.append(kwargs["json_body"])
        return {"code": 0, "data": {"dirId": "profile-1"}}

    with patch.object(client, "request", side_effect=request), \\
            patch.object(roxy_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", False), \\
            patch.object(roxy_cfg, "ROXY_RANDOM_OS_ON_CREATE", False), \\
            patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "135422"):
        self.assertEqual(client.create_profile(), "profile-1")

    self.assertTrue(calls[0]["positionSwitch"])
    self.assertEqual(calls[0]["windowRatioPosition"], "0,0")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `\.venv\Scripts\python.exe -m pytest tests/test_roxy_window_position.py::test_create_profile_requests_first_display_position -q`

Expected: FAIL because the current default `ROXY_PROFILE_CREATE_PAYLOAD` has no window-position fields.

- [ ] **Step 3: Write minimal implementation**

将 `config/roxybrowser.py` 的默认 payload 改为：

```python
ROXY_PROFILE_CREATE_PAYLOAD: dict = {
    "name": "gpt-free-register",
    "os": "macOS",
    "positionSwitch": True,
    "windowRatioPosition": "0,0",
}
```

不要在 `ROXY_OPEN_EXTRA_PARAMS["args"]` 中加入 `--window-position=0,0`。

- [ ] **Step 4: Run test to verify it passes**

Run: `\.venv\Scripts\python.exe -m pytest tests/test_roxy_window_position.py::test_create_profile_requests_first_display_position -q`

Expected: PASS，且请求日志中的创建 payload 能看到两个位置字段。

- [ ] **Step 5: Checkpoint 1**

使用一个全新的 Roxy Profile 创建请求，检查 Roxy 窗口详情中保存的窗口位置为第一个显示器左上角；旧 Profile 不会被该默认值自动迁移，需在 Task 3 处理。

### Task 2: 在 Selenium 连接前按 PID 预定位窗口

**Files:**
- Create: `core/windows_window.py`
- Modify: `core/roxybrowser_client.py:25-33, 594-648`
- Modify: `core/roxy_registration.py:56-91`
- Create: `tests/test_windows_window.py`
- Modify: `tests/test_roxy_window_position.py`

**Interfaces:**
- Consumes: `/browser/open` 返回的 `data.pid`，以及 Windows User32 的可见顶层窗口。
- Produces: `RoxyOpenResult.process_id: int | None`；`move_process_window_to_primary(process_id, timeout=2.0)` 在 Selenium 连接前完成窗口移动。

- [ ] **Step 1: Write the failing tests**

```python
def test_open_profile_extracts_roxy_process_id():
    client = RoxyBrowserClient()
    response = {
        "code": 0,
        "data": {
            "pid": 24680,
            "ws": "ws://127.0.0.1:52314/devtools/browser/test",
        },
    }
    with patch.object(client, "create_profile", return_value="profile-1"), \\
            patch.object(client, "request", return_value=response), \\
            patch.object(roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True):
        opened = client.open_profile()

    self.assertEqual(opened.process_id, 24680)
```

```python
def test_center_position_uses_primary_work_area():
    self.assertEqual(
        calculate_center_position((0, 0, 1920, 1080), (1000, 800)),
        (460, 140),
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `\.venv\Scripts\python.exe -m pytest tests/test_roxy_window_position.py tests/test_windows_window.py -q`

Expected: FAIL because `RoxyOpenResult` has no `process_id` and `core/windows_window.py` does not exist.

- [ ] **Step 3: Implement the minimal native positioning seam**

在 `core/roxybrowser_client.py` 中：

```python
@dataclass
class RoxyOpenResult:
    profile_id: str
    raw: dict
    debugger_address: str | None = None
    webdriver_url: str | None = None
    ws_endpoint: str | None = None
    process_id: int | None = None
    created_by_run: bool = False
```

从以下响应路径提取正整数 PID：`pid`、`processId`、`data.pid`、`data.processId`，并在 `open_profile()` 返回 `RoxyOpenResult` 时写入 `process_id`。

在 `core/windows_window.py` 中提供：

```python
def calculate_center_position(
    work_area: tuple[int, int, int, int],
    window_size: tuple[int, int],
) -> tuple[int, int]: ...

def move_process_window_to_primary(
    process_id: int | None,
    *,
    timeout: float = 2.0,
    poll_interval: float = 0.05,
) -> bool: ...
```

实现要求：

1. 非 Windows、空 PID、PID 不存在时直接返回 `False`，不得影响注册流程。
2. 用 `EnumDisplayMonitors`/`GetMonitorInfoW` 找到 `MONITORINFOF_PRIMARY` 对应的工作区。
3. 用 `EnumWindows`、`GetWindowThreadProcessId`、`IsWindowVisible` 找到该 PID 的可见顶层窗口。
4. 找到窗口后读取现有宽高，通过 `SetWindowPos` 移到主屏工作区中央，使用 `SWP_NOACTIVATE`，避免抢焦点。
5. 用截止时间轮询窗口出现，不使用无限循环或固定长时间睡眠。

在 `core/roxy_registration.py::_build_driver()` 的两个 Selenium 分支前调用：

```python
move_process_window_to_primary(opened.process_id)
```

当前 `_center_browser_window(driver)` 保留，作为连接成功后的最终兜底；它不再承担首次定位职责。

- [ ] **Step 4: Run tests to verify they pass**

Run: `\.venv\Scripts\python.exe -m pytest tests/test_roxy_window_position.py tests/test_windows_window.py -q`

Expected: PASS；在非 Windows CI 中，原生定位函数被隔离为 `False`，不会导致测试失败。

- [ ] **Step 5: Checkpoint 2**

在日志中确认顺序为：`/browser/open` 返回 PID → `[Roxy窗口] 预定位主屏` → Selenium 连接 → `_center_browser_window` 兜底。若没有 PID，只允许记录一次“无法预定位”，不能阻塞注册。

### Task 3: 处理旧 Profile 与配置入口

**Files:**
- Modify: `core/roxybrowser_client.py`
- Modify: `config/roxybrowser.py`
- Modify: `webui/config_editor.py`（仅在现有配置编辑器需要展示开关时）
- Create: `tests/test_roxy_profile_window_migration.py`

**Interfaces:**
- Consumes: 已存在的 `ROXY_PROFILE_ID` 或历史创建的 Profile。
- Produces: 可选的一次性 `/browser/mdf` 更新，将旧 Profile 的窗口位置改成 `positionSwitch=true`、`windowRatioPosition="0,0"`。

- [ ] **Step 1: Write the failing test**

验证 `RoxyBrowserClient.update_profile_window_position("profile-1")` 发出的请求体为：

```python
{
    "workspaceId": 135422,
    "dirId": "profile-1",
    "positionSwitch": True,
    "windowRatioPosition": "0,0",
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `\.venv\Scripts\python.exe -m pytest tests/test_roxy_profile_window_migration.py -q`

Expected: FAIL because当前客户端没有 `/browser/mdf` 封装。

- [ ] **Step 3: Implement migration method**

新增方法，默认使用 `POST /browser/mdf`；仅在用户明确开启 `ROXY_ENFORCE_PRIMARY_WINDOW_POSITION=True` 时，在打开旧 Profile 前调用。默认关闭，避免每次启动都修改远端 Profile。

- [ ] **Step 4: Run test to verify it passes**

Run: `\.venv\Scripts\python.exe -m pytest tests/test_roxy_profile_window_migration.py -q`

Expected: PASS。

- [ ] **Step 5: Checkpoint 3**

先对一个测试 Profile 执行迁移，再用同一 Profile 打开 3 次；确认不再保存第二显示器的位置。确认后再批量迁移其它 Profile。

### Task 4: 全量验证与回归检查

**Files:**
- No production file changes; use the tests and logs from Tasks 1–3.

- [ ] **Step 1: Run focused tests**

Run: `\.venv\Scripts\python.exe -m pytest tests/test_roxy_window_position.py tests/test_windows_window.py tests/test_roxy_profile_window_migration.py -q`

Expected: 所有窗口定位和 API payload 测试通过。

- [ ] **Step 2: Run the full suite**

Run: `\.venv\Scripts\python.exe -m pytest -q`

Expected: 现有测试全部通过，且没有改变 OTP、注册、密码设置和 WebUI 行为。

- [ ] **Step 3: Manual two-monitor acceptance test**

将笔记本屏幕设为 Windows 主显示器，设置 `ROXY_OPEN_HEADLESS=False`，使用新 Profile 连续启动 10 次；验收标准：

1. 窗口首次可见位置在笔记本屏幕，不在第二屏闪现。
2. 日志中的 `windowRatioPosition` 为 `0,0`。
3. 若存在 Selenium 兜底移动，目标坐标仍落在主显示器工作区。
4. headless 模式仍不显示窗口，非 Windows 环境不报错。

- [ ] **Step 4: Commit**

```bash
git add config/roxybrowser.py core/roxybrowser_client.py core/roxy_registration.py core/windows_window.py tests/test_roxy_window_position.py tests/test_windows_window.py tests/test_roxy_profile_window_migration.py
git commit -m "fix: launch Roxy windows on primary monitor"
```
