# 套餐地区与支付会话识别 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为套餐结果显示资格地区和实际出口地区，并在提链结果中显示识别到的 `oaics_` 或 `cs_` 支付会话类型。

**Architecture:** 套餐解析器只读取 `accounts/check` 响应中的明确地区字段；套餐 HTTP 会话额外执行可失败的轻量 GeoIP 查询，记录最终出口位置。支付检测器作为独立的纯函数模块接入提链 SSE 结果，数据库只保存派生识别字段，WebUI 分别在套餐列和提链列渲染。

**Tech Stack:** Python 3、`unittest`/pytest、Flask WebUI、现有 `curl_cffi` HTTP 会话、原生 JavaScript 模板。

## Global Constraints

- 不发起支付、不创建 Checkout Session、不提交银行卡或支付方式。
- 支付检测器只解析内存中的 JSON，不联网、不读取 Token、不保存完整支付载荷。
- 套餐资格地区与出口 IP 地区必须分开保存和展示；资格地区缺失时显示“未返回”，不得用出口地区回填。
- GeoIP 失败不能导致套餐查询失败。
- 不改变现有提链请求、SSE 协议、CDK 扣次逻辑和套餐查询重试策略。
- 修改必须使用测试先行：先写失败测试，确认失败后再写生产代码。

---

### Task 1: 增加套餐资格地区解析

**Files:**
- Modify: `core/chatgpt_plan.py:194-275`
- Test: `tests/test_chatgpt_plan.py`（若不存在则创建）

**Interfaces:**
- Produces `plan_eligibility_country`, `plan_eligibility_region`, `plan_eligibility_region_source` in `parse_accounts_check` results.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_accounts_check_extracts_explicit_eligibility_region():
    payload = {
        "accounts": {
            "default": {
                "account": {"account_id": "acct-1", "plan_type": "free"},
                "eligible_promo_campaigns": {"plus": {"id": "plus-1"}},
                "country": "JP",
                "region": "Kanto",
            }
        }
    }
    result = parse_accounts_check(payload)
    assert result["plan_eligibility_country"] == "JP"
    assert result["plan_eligibility_region"] == "Kanto"
    assert result["plan_eligibility_region_source"] == "accounts_check"


def test_parse_accounts_check_does_not_infer_missing_eligibility_region():
    payload = {"accounts": {"default": {"account": {"plan_type": "free"}}}}
    result = parse_accounts_check(payload)
    assert result.get("plan_eligibility_country") is None
    assert result.get("plan_eligibility_region") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_chatgpt_plan.py -q`

Expected: FAIL because the result does not contain the new region keys.

- [ ] **Step 3: Implement the minimal parser**

Add a helper that checks the selected account item and its `account`/`entitlement`/campaign metadata for the ordered country keys `country`, `country_code`, `countryCode`, `billing_country`, `billingCountry`, and region keys `region`, `region_code`, `regionCode`, `residency_region`, `residencyRegion`. Add the three result keys without changing existing plan eligibility logic.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_chatgpt_plan.py -q`

Expected: PASS.

---

### Task 2: Add safe exit GeoIP collection to plan checks

**Files:**
- Modify: `core/chatgpt_plan.py:311-455`
- Test: `tests/test_chatgpt_plan.py`

**Interfaces:**
- Add `_detect_plan_exit_geo(session) -> dict`.
- `check_account_plan` returns `plan_exit_ip`, `plan_exit_country`, `plan_exit_region`, `plan_exit_city`, `plan_exit_timezone`, and `plan_exit_geo_source` when available.

- [ ] **Step 1: Write the failing tests**

```python
def test_check_account_plan_keeps_success_when_exit_geo_fails(monkeypatch):
    monkeypatch.setattr("core.chatgpt_plan._detect_plan_exit_geo", lambda session: {})
    # Use the existing response/session test seam in this file to return a valid accounts/check payload.
    result = check_account_plan("valid-token", proxy="", max_attempts=1)
    assert result["ok"] is True
    assert "plan_exit_country" not in result


def test_exit_geo_normalizes_country_and_location():
    class Response:
        status_code = 200
        def json(self):
            return {"ip": "203.0.113.10", "country": "jp", "region": "Kanto", "city": "Tokyo", "timezone": "Asia/Tokyo"}

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    geo = _detect_plan_exit_geo(Session())
    assert geo == {
        "plan_exit_ip": "203.0.113.10",
        "plan_exit_country": "JP",
        "plan_exit_region": "Kanto",
        "plan_exit_city": "Tokyo",
        "plan_exit_timezone": "Asia/Tokyo",
        "plan_exit_geo_source": "ipinfo.io",
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_chatgpt_plan.py -q`

Expected: FAIL because `_detect_plan_exit_geo` is not defined.

- [ ] **Step 3: Implement minimal GeoIP support**

Use `config.browser.IP_GEO_ENDPOINTS` and `IP_GEO_TIMEOUT`. For each endpoint, call `session.get(url, headers={"Accept": "application/json"}, timeout=timeout)`, accept only HTTP 200 JSON objects, normalize the country code to uppercase, and continue to the next endpoint on all exceptions. Call this helper from the existing plan-check session before the accounts/check request. Merge the returned fields into both successful and failed result metadata only when non-empty.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_chatgpt_plan.py -q`

Expected: PASS, including the existing retry and expired-token tests.

---

### Task 3: Create the offline payment session detector

**Files:**
- Create: `core/payment_method_detector.py`
- Test: `tests/test_payment_method_detector.py`

**Interfaces:**
- `parse_checkout_session(payload, *, billing_country, fallback_publishable_key="") -> CheckoutSessionInfo`
- `parse_capability_evidence(payload, *, fallback_currency="") -> CapabilityEvidence`
- `classify_payment_method(evidence, expected_method) -> tuple[str, bool | None]`
- `detect_oaics(checkout_payload, stripe_init_payload=None, *, billing_country, fallback_currency="", expected_method="paypal") -> dict`
- `detect_extract_payment_session(extract_payload, *, billing_country="", fallback_currency="", expected_method="") -> dict`

- [ ] **Step 1: Write the failing tests**

```python
def test_detect_oaics_identifies_oaics_session():
    result = detect_oaics({"checkout_session_id": "oaics_test123"}, billing_country="JP")
    assert result["detected"] is True
    assert result["is_oaics"] is True
    assert result["session_kind"] == "oaics"


def test_detect_oaics_identifies_stripe_cs_session_and_method():
    result = detect_oaics(
        {"id": "cs_test123"},
        {"payment_method_types": ["card", "paypal"], "currency": "jpy", "amount_due": 0},
        billing_country="JP",
        expected_method="paypal",
    )
    assert result["session_kind"] == "stripe_cs"
    assert result["method_status"] == "available"
    assert result["offer_state"] == "zero_due"


def test_nested_extract_result_without_supported_session_is_not_detected():
    result = detect_extract_payment_session({"result": {"id": "job-123", "payment_method": "pix"}})
    assert result["detected"] is False
    assert result["session_kind"] == "unknown"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_payment_method_detector.py -q`

Expected: FAIL because the module and public functions do not exist.

- [ ] **Step 3: Implement the detector**

Port the attached offline parser into the new module. Keep the dataclasses and alias normalization. Add a depth-limited recursive walk that accepts only session IDs beginning with `oaics_` or `cs_`; search common nested checkout/stripe keys and, as a fallback, scan nested dictionaries for a supported session ID. Return `detected: false` instead of raising when no session exists. Never call an HTTP client in this module.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_payment_method_detector.py -q`

Expected: PASS.

---

### Task 4: Connect payment detection and save derived fields

**Files:**
- Modify: `core/extract_link_service.py:260-315`
- Modify: `core/db.py:1016-1055`
- Test: `tests/test_extract_link_payment_detection.py`

**Interfaces:**
- SSE result handling adds `payment_detection` to the final task result.
- DB stores only derived payment fields and leaves existing raw result storage unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_update_account_extract_saves_payment_session_fields(tmp_path, monkeypatch):
    # Point the DB test seam at one temporary account containing id=7.
    update_account_extract(7, {
        "ok": True,
        "status": "success",
        "result": {"long_url": "https://example.test/pay"},
        "payment_detection": {
            "detected": True,
            "checkout_session_id": "oaics_test123",
            "session_kind": "oaics",
            "is_oaics": True,
            "method_status": "available",
            "method_available": True,
            "payment_method_types": ["paypal"],
            "currency": "JPY",
            "amount_minor": 0,
            "offer_state": "zero_due",
        },
    })
    row = get_account(7)
    assert row["extract_link_payment_session_kind"] == "oaics"
    assert row["extract_link_payment_is_oaics"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_extract_link_payment_detection.py -q`

Expected: FAIL because DB does not save the derived fields.

- [ ] **Step 3: Implement the connection**

In the SSE `result` branch, call `detect_extract_payment_session(result, expected_method=link_type)` inside a narrow `try/except`; put its return value on `payment_detection` and continue on detector errors. In `db.update_account_extract`, read `result["payment_detection"]` and map each derived field to the `extract_link_payment_*` columns in the account JSON. Use `json.dumps` for the method list only. Add no raw payment body beyond the existing `extract_link_result_json`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_extract_link_payment_detection.py -q`

Expected: PASS, including the existing extract-link service tests.

---

### Task 5: Expose region and payment fields through the WebUI API

**Files:**
- Modify: `webui/app.py:110-145`
- Modify: `core/db.py:1154-1225`
- Test: `tests/test_webui_account_features.py`

**Interfaces:**
- Account list rows expose `plan_eligibility_*`, `plan_exit_*`, and `extract_link_payment_*` only when populated.

- [ ] **Step 1: Write the failing tests**

```python
def test_account_list_exposes_region_and_payment_detection_fields(client, monkeypatch):
    monkeypatch.setattr("webui.app.db.list_accounts_page", lambda **kwargs: {
        "items": [{
            "id": 7, "email": "user@example.com",
            "plan_eligibility_country": "JP",
            "plan_exit_country": "JP",
            "plan_exit_city": "Tokyo",
            "extract_link_payment_detected": True,
            "extract_link_payment_session_kind": "oaics",
            "extract_link_payment_session_id": "oaics_test123",
        }], "total": 1, "sources": [], "revision": "1:now",
    })
    response = client.get("/api/accounts?paged=1&page=1&page_size=20")
    payload = response.get_json()
    row = payload["items"][0]
    assert row["plan_eligibility_country"] == "JP"
    assert row["extract_link_payment_session_kind"] == "oaics"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_webui_account_features.py -q`

Expected: FAIL because the compact API omits the new fields.

- [ ] **Step 3: Implement API exposure**

Add the new keys to the compact account optional-field list and the lightweight plan-status snapshot where required. Preserve omission of empty values and sensitive fields.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_webui_account_features.py -q`

Expected: PASS.

---

### Task 6: Render both regions and session recognition in the account table

**Files:**
- Modify: `webui/templates/index.html:3197-3300`
- Modify: `webui/templates/index_legacy.html:1300-1385` (keep the legacy page behavior aligned)
- Test: `tests/test_webui_account_features.py`

**Interfaces:**
- `_planCell(row)` renders separate eligibility and exit region labels.
- `_extractLinkCell(row)` renders `OAICS`, `Stripe cs_`, or “未识别” without removing existing link controls.

- [ ] **Step 1: Write the failing tests**

```python
def test_account_template_mentions_region_and_session_labels():
    html = (Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "资格地区" in html
    assert "出口地区" in html
    assert "OAICS" in html
    assert "Stripe cs_" in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_webui_account_features.py::test_account_template_mentions_region_and_session_labels -q`

Expected: FAIL because the labels are not in the template.

- [ ] **Step 3: Implement the rendering**

In `_planCell`, keep the pill text `free(可Plus试用)` and add a compact detail block below it. Use `资格地区: 未返回` when no eligibility country/region exists and `出口地区: 未知` when no exit fields exist. Escape every dynamic value with the existing `esc` helper.

In `_extractLinkCell`, build a `paymentSessionHtml` block for successful results. Render `会话: OAICS (id)` for `session_kind === 'oaics'`, `会话: Stripe cs_ (id)` for `session_kind === 'stripe_cs'`, and `会话: 未识别` when `extract_link_payment_detected === false`. Keep copy, QR and expiry controls unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_webui_account_features.py -q`

Expected: PASS.

---

### Task 7: Run the complete regression suite and perform a manual smoke check

**Files:**
- Test only: existing `tests/` suite

- [ ] **Step 1: Run focused tests**

Run: `pytest tests/test_chatgpt_plan.py tests/test_payment_method_detector.py tests/test_extract_link_payment_detection.py tests/test_webui_account_features.py -q`

Expected: PASS.

- [ ] **Step 2: Run the full suite**

Run: `pytest -q`

Expected: all existing and new tests pass; no payment request is made by the detector tests.

- [ ] **Step 3: Manually verify the UI data states**

Verify these fixtures in the account table:

```text
free(可Plus试用) / 资格地区: JP / 出口地区: JP / Tokyo
free(可Plus试用) / 资格地区: 未返回 / 出口地区: 未知
提链成功(PIX) / 会话: OAICS (oaics_...)
提链成功(PIX) / 会话: Stripe cs_ (cs_...)
提链成功(PIX) / 会话: 未识别
```

- [ ] **Step 4: Record verification result**

Document the test command output and note that this workspace is not a Git repository, so no commit can be created unless the repository metadata is restored.

