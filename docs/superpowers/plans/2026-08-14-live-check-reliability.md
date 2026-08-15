# 查活可靠性改造实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提高已注册账号查活的成功率，并确保网络 403/429 不会被误判为账号失效。

**Architecture:** 将查活拆成“网络路由选择、单会话预检、认证流程、结果分类”四层。每次尝试必须记录实际出口和网络结果；代理 403 不再自动切换直连，而是遵守冷却并按明确的路由策略重试。账号只有在认证阶段收到明确的账号不可用信号后才标记为废号。

**Tech Stack:** Python、`curl_cffi`、现有 `BrowserSession`、ThreadPoolExecutor、pytest。

## Global Constraints

- 不通过轮换 IP、伪造指纹或高频请求规避第三方风控；只使用已获授权且稳定的网络出口。
- Providers/CSRF 阶段的 403/429 必须分类为 `network_unavailable` 或可重试失败，不得分类为 `deactivated`。
- 同一次账号查活不得在代理失败后自动切换直连；代理和直连必须作为独立、明确的路由策略。
- 不记录 OTP、access token、完整 Cookie 或代理认证密码。
- 现有用户未提交的改动必须保留。

---

### Task 1: 建立网络错误分类和重试策略

**Files:**
- Modify: `core/account_liveness.py:20-75`
- Modify: `core/session.py:460-500`
- Create: `tests/test_account_liveness_network.py`

**Interfaces:**
- Produces `_classify_network_error(exc) -> str`，返回 `forbidden`、`rate_limited`、`transient` 或 `unknown`。
- Produces `_network_preflight_with_retry(email, proxy, max_attempts=4)`，保持现有返回类型，但失败时保留最后一次网络分类和冷却信息。

- [ ] **Step 1: 写失败测试，验证 403 不会立即密集重试**

```python
from core.account_liveness import _network_preflight_with_retry


def test_preflight_403_stops_until_cooldown(monkeypatch):
    calls = []

    class FakeResponseError(Exception):
        status_code = 403

    def fake_get_providers(session):
        calls.append(session.proxy)
        raise FakeResponseError("HTTP Error 403")

    monkeypatch.setattr("core.account_liveness.get_providers", fake_get_providers)
    monkeypatch.setattr("core.account_liveness.BrowserSession", lambda proxy: type(
        "S", (), {"proxy": proxy, "session": type("HTTP", (), {"close": lambda self: None})()}
    )())
    monkeypatch.setattr("core.account_liveness.time.sleep", lambda seconds: None)

    with pytest.raises(Exception):
        _network_preflight_with_retry("a@example.com", "http://127.0.0.1:11828", max_attempts=1)

    assert len(calls) == 1
```

- [ ] **Step 2: 运行测试确认当前行为未覆盖网络分类**

运行：`pytest tests/test_account_liveness_network.py -q`

预期：测试先失败或无法导入待新增的分类接口。

- [ ] **Step 3: 增加统一错误分类**

根据异常类型、`status_code`、`Retry-After` 和错误文本分类。优先读取响应状态码，不依赖字符串中是否包含 `403`。分类结果只用于调度和状态记录，不用于判定账号废号。

- [ ] **Step 4: 实现指数退避和冷却边界**

建议参数：

```python
BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 900
```

403：停止当前会话，等待由冷却策略决定的时间后才允许下一次任务；429：读取 `Retry-After`，没有该字段时使用指数退避；连接重置/超时：使用 5、10、20 秒退避。测试中通过 monkeypatch 时间函数，不让测试真实等待。

- [ ] **Step 5: 运行测试确认通过**

运行：`pytest tests/test_account_liveness_network.py -q`

预期：PASS，并能区分 403、429、超时三类错误。

- [ ] **Step 6: 提交独立变更**

```powershell
git add tests/test_account_liveness_network.py core/account_liveness.py core/session.py
git commit -m "fix: classify and back off live-check network failures"
```

### Task 2: 修正路由选择，取消代理 403 后的直连混用

**Files:**
- Modify: `core/live_check_service.py:41-80`
- Modify: `core/chatgpt_plan.py:79-136`
- Modify: `config/proxy.py`
- Create: `tests/test_live_check_service.py`

**Interfaces:**
- `resolve_plan_check_route()` 继续返回 `proxy`、`network_route`、`proxy_mode`、`proxy_used`、`proxy_fallback_reason`。
- `_run_live_check()` 不再根据账号查活结果临时改变网络路由。

- [ ] **Step 1: 写失败测试，验证代理 403 不会自动直连**

```python
from types import SimpleNamespace

from core.live_check_service import _run_live_check


def test_proxy_403_does_not_fallback_to_direct(monkeypatch):
    routes = []

    def fake_check(email, proxy=None, clear_log=True):
        routes.append(proxy)
        return {"ok": False, "status": "failed", "error": "HTTPError: HTTP Error 403"}

    monkeypatch.setattr("core.live_check_service.check_account_liveness", fake_check)
    monkeypatch.setattr("core.live_check_service.resolve_plan_check_route", lambda explicit_proxy=None: {
        "proxy": "http://127.0.0.1:11828",
        "network_route": "proxy",
        "proxy_mode": "proxy",
        "proxy_used": "http://127.0.0.1:11828",
        "proxy_fallback_reason": None,
    })
    monkeypatch.setattr("core.live_check_service.db.mark_account_live_check_running", lambda account_id: True)
    monkeypatch.setattr("core.live_check_service.db.update_account_liveness", lambda account_id, result: None)
    monkeypatch.setattr("core.live_check_service._append_log", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.live_check_service._QUEUE_SLOTS", SimpleNamespace(release=lambda: None))

    result = _run_live_check(account_id=1, email="a@example.com", proxy=None, trigger="manual")
    assert routes == ["http://127.0.0.1:11828"]
    assert result["status"] == "failed"
```

- [ ] **Step 2: 删除 `live_check_service.py` 中“代理 403 后直连兜底一次”的分支**

代理模式失败后只记录 `network_unavailable`，写入账号最近一次查活错误，但不触发第二套出口。`auto` 只允许在本地代理端口不可用时，在任务开始前选择直连；不能因为远端返回 403 才切换。

- [ ] **Step 3: 明确代理池语义**

在 `config/proxy.py` 中补充配置说明：

```python
# 查活使用稳定且获授权的出口；同一任务不在 proxy/direct 间切换。
# 若池中多个地址实际共享同一上游出口，不应把它们当作不同 IP。
```

保留 `PLAN_CHECK_PROXY_MODE=auto/proxy/direct`，但让 `proxy` 模式在没有可用代理时直接返回配置错误，避免静默直连。

- [ ] **Step 4: 增加网络状态字段**

在查活结果中增加 `failure_kind`，至少支持 `network_unavailable`、`otp_invalid`、`account_unusable`、`unknown`。数据库写回时保留原有 `live_check_error`，不要修改既有账号状态语义。

- [ ] **Step 5: 运行测试**

运行：`pytest tests/test_live_check_service.py tests/test_plan_check_service.py -q`

预期：代理 403 只产生一次代理路由调用，不出现 `proxy=""` 的直连回退调用。

- [ ] **Step 6: 提交独立变更**

```powershell
git add tests/test_live_check_service.py core/live_check_service.py core/chatgpt_plan.py config/proxy.py
git commit -m "fix: keep live-check network route stable"
```

### Task 3: 记录真实出口和避免错误的 GeoIP 缓存

**Files:**
- Modify: `core/session.py:159-198`
- Modify: `core/account_liveness.py:50-63`
- Create: `tests/test_session_exit_geo.py`

**Interfaces:**
- `BrowserSession` 提供当前会话的 `exit_geo`，至少包含 `ip`、`country`、`timezone`；缺失时使用 `None`，不能伪造。
- 日志只记录 IP、国家和城市等诊断信息，不记录代理凭据。

- [ ] **Step 1: 写失败测试，验证相同代理 URL 不应永久复用旧出口信息**

```python
def test_exit_geo_cache_expires(monkeypatch):
    from core.session import BrowserSession

    now = [100.0]
    responses = iter([
        {"ip": "203.0.113.1", "country": "JP", "timezone": "Asia/Tokyo"},
        {"ip": "203.0.113.2", "country": "US", "timezone": "America/Los_Angeles"},
    ])
    monkeypatch.setattr("core.session.time.time", lambda: now[0])
    monkeypatch.setattr("core.session._GEO_CACHE_TTL_SECONDS", 60.0)
    # 两次检测使用同一代理 URL，但模拟上游出口 IP 变化。
    # 第二次检测在 TTL 过期后必须重新请求，而不是直接返回旧 IP。
    first = BrowserSession._get_cached_exit_geo(
        "http://127.0.0.1:11828", lambda: next(responses)
    )
    assert first["ip"] == "203.0.113.1"

    now[0] = 161.0
    second = BrowserSession._get_cached_exit_geo(
        "http://127.0.0.1:11828", lambda: next(responses)
    )
    assert second["ip"] == "203.0.113.2"
```

- [ ] **Step 2: 将 GeoIP 缓存改为带 TTL 的诊断缓存**

缓存键仍可使用代理 URL，但必须设置短 TTL；缓存只用于浏览器地区画像，不能作为“本次真实出口 IP 已确认”的依据。每个查活会话至少在日志中记录一次真实出口检测结果。

- [ ] **Step 3: 为预检日志增加结构化字段**

在会话创建日志中增加：`attempt`、`route`、`exit_ip`、`country`、`status`。对于获取 Providers 的 403，记录响应状态和可用的 Cloudflare Ray ID；不记录响应正文中的敏感信息。

- [ ] **Step 4: 运行测试**

运行：`pytest tests/test_session_exit_geo.py -q`

预期：PASS；日志测试确认不存在 OTP、Token 和代理密码。

- [ ] **Step 5: 提交独立变更**

```powershell
git add tests/test_session_exit_geo.py core/session.py core/account_liveness.py
git commit -m "feat: observe live-check exit network accurately"
```

### Task 4: 降低查活并发并增加任务级冷却

**Files:**
- Modify: `core/live_check_service.py:13-20`
- Modify: `config/proxy.py`
- Modify: `webui/config_editor.py`
- Create: `tests/test_live_check_throttle.py`

**Interfaces:**
- 增加可配置的 `LIVE_CHECK_WORKERS`、`LIVE_CHECK_MIN_INTERVAL`、`LIVE_CHECK_403_COOLDOWN`。
- 现有队列 API 保持兼容。
- 增加 `_LiveCheckThrottle.wait_for_slot(now: float | None = None) -> float`；该方法返回还需等待的秒数，并更新下一次允许启动的时间。

- [ ] **Step 1: 写失败测试，验证任务不会并发冲击同一出口**

```python
from core.live_check_service import _LiveCheckThrottle


def test_live_check_global_throttle():
    throttle = _LiveCheckThrottle(min_interval=30.0)

    assert throttle.wait_for_slot(now=100.0) == 0.0
    assert throttle.wait_for_slot(now=100.0) == 30.0
    assert throttle.wait_for_slot(now=130.0) == 0.0
```

- [ ] **Step 2: 默认将查活 worker 数设置为 1**

查活登录包含 Providers、CSRF、OAuth 和 OTP，多账号并发会显著增加同一出口的风控压力。先采用 1 个 worker；确认稳定后再逐步提高，不超过 2。

- [ ] **Step 3: 增加任务级最小间隔和 403 冷却**

建议默认值：

```python
LIVE_CHECK_WORKERS = 1
LIVE_CHECK_MIN_INTERVAL = 30.0
LIVE_CHECK_403_COOLDOWN = 900.0
```

任务因 403 失败时，不立即重新入队；返回“网络暂不可用，请稍后重试”，并在日志中记录下一次可重试时间。

- [ ] **Step 4: 在 WebUI 中展示失败类别**

将 `network_unavailable` 显示为“网络暂不可用”，将 `account_unusable` 显示为“账号不可用”，避免用户看到“查活失败”后误以为邮箱已经废掉。

- [ ] **Step 5: 运行测试**

运行：`pytest tests/test_live_check_throttle.py tests/test_webui_account_features.py -q`

预期：PASS，且现有队列、状态刷新测试不回归。

- [ ] **Step 6: 提交独立变更**

```powershell
git add tests/test_live_check_throttle.py core/live_check_service.py config/proxy.py webui/config_editor.py
git commit -m "feat: throttle live-check jobs"
```

### Task 5: 完整回归和上线验收

**Files:**
- Modify: `docs/` 中的查活说明文档（如已有对应文档则更新）
- Test: `tests/test_account_liveness_network.py`
- Test: `tests/test_live_check_service.py`
- Test: `tests/test_live_check_throttle.py`

- [ ] **Step 1: 运行查活相关测试**

```powershell
pytest tests/test_account_liveness_network.py tests/test_live_check_service.py tests/test_live_check_throttle.py tests/test_plan_check_service.py -q
```

- [ ] **Step 2: 运行全量测试**

```powershell
pytest -q
```

- [ ] **Step 3: 做一次小批量人工验收**

只选 2 个已知正常账号，间隔至少 30 秒，确认日志顺序为：

```text
route selected
exit IP observed
Providers success
CSRF success
OTP success
OAuth callback success
Session success
```

- [ ] **Step 4: 验证网络失败不会改成废号**

使用一个会返回 403 的测试桩，确认数据库状态为 `failed/network_unavailable`，而不是 `deactivated`。

- [ ] **Step 5: 检查敏感信息泄漏**

检查查活日志和 WebUI 返回值中不包含 OTP、access token、Cookie 值和代理用户名密码。

- [ ] **Step 6: 记录上线参数**

上线初始参数：worker=1、任务间隔=30 秒、403 冷却=900 秒。连续观察一批任务后，再根据真实出口 403 比例调整；不通过提高并发或缩短冷却来“硬冲”成功率。

## 验收标准

1. Providers 阶段 403 不再触发代理到直连的自动切换。
2. 同一查活任务的日志能够显示实际网络路由和出口 IP。
3. 403/429/超时具有不同的重试和冷却行为。
4. 网络失败不会把账号标记为废号。
5. 已知正常账号在稳定出口、低并发条件下可以完整走完 OAuth、OTP 和 Session 流程。
6. 所有新增测试和现有全量测试通过。
