# 账号页与注册元数据改进实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 在不暴露敏感值的前提下，完善账号密码操作、通用 API 取件地址复制、账号选择计数、注册 IP 展示，并阻止注册 OTP 被设置密码流程复用。

**Architecture:** 账号列表继续使用紧凑响应，完整取件地址和注册密码统一走按需 secret 接口。注册驱动在保存账号时写入可选 registration_ip；套餐单元格只消费该元数据。Roxy 设置密码流程接收本次注册已提交的 OTP，通用 API 取码客户端过滤旧码并在必要时触发重发。

**Tech Stack:** Python 3、Flask、原生 HTML/CSS/JavaScript、unittest、现有 JSON 文件持久化、Selenium/Playwright 浏览器驱动。

## Global Constraints

- 只有 generic_api 来源允许复制取件地址；Outlook、Cloudflare 等来源不新增该展示。
- 普通 /api/accounts 紧凑列表不得返回完整 code_url、明文密码或其他现有敏感字段。
- registration_ip 是可选字段，历史账号缺失时页面不显示空行，IP 探测失败不能使注册失败。
- 注册 OTP 只在当前任务内存中传递，不写入账号记录、日志、错误响应或测试输出。
- 不改变现有密码生成规则、密码设置队列并发策略、账号选择器和邮箱池删除行为。
- 每个任务完成后运行其专属测试；实现阶段按任务边界提交小步 commit。

---

### Task 1: 建立后端敏感字段与列表响应的失败测试

Files:
- Modify: tests/test_webui_account_features.py

Interfaces:
- Consumes: webui.app._compact_account_for_list()、/api/accounts/<id>/secret。
- Produces: pickup_address_available 列表字段和 pickup_address secret 字段的测试契约。

- [ ] Step 1: 写列表脱敏和 secret 读取的失败测试。

~~~python
@patch("webui.app.db.get_generic_api_email_by_email")
@patch("webui.app.db.list_accounts_page")
def test_generic_api_pickup_address_is_available_but_not_in_compact_row(
    self, list_page, get_pool
):
    list_page.return_value = {
        "items": [{"id": 8, "email": "pool@example.com", "email_source": "generic_api"}],
        "total": 1, "sources": [], "revision": "1:now",
    }
    get_pool.return_value = {
        "email": "pool@example.com",
        "code_url": "https://mail.example/messages/token",
    }
    response = self.client.get("/api/accounts?paged=1&page=1&page_size=20")
    self.assertEqual(response.status_code, 200)
    row = response.get_json()["items"][0]
    self.assertTrue(row["pickup_address_available"])
    self.assertNotIn("pickup_address", row)
    self.assertNotIn("https://mail.example/messages/token",
                     response.get_data(as_text=True))

@patch("webui.app.db.get_generic_api_email_by_email")
@patch("webui.app.db.get_account")
def test_pickup_address_secret_is_only_available_for_generic_api(
    self, get_account, get_pool
):
    get_account.return_value = {
        "id": 8, "email": "pool@example.com", "email_source": "generic_api"
    }
    get_pool.return_value = {
        "email": "pool@example.com",
        "code_url": "https://mail.example/messages/token",
    }
    response = self.client.get("/api/accounts/8/secret?field=pickup_address")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.get_json()["value"],
                     "https://mail.example/messages/token")

@patch("webui.app.db.get_account")
def test_pickup_address_secret_rejects_non_generic_api_account(self, get_account):
    get_account.return_value = {
        "id": 9, "email": "outlook@example.com", "email_source": "outlook",
        "pickup_address": "https://should-not-be-exposed.example/otp",
    }
    response = self.client.get("/api/accounts/9/secret?field=pickup_address")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.get_json()["value"], "")
~~~

- [ ] Step 2: 运行测试确认当前实现失败。

Run: python -m pytest tests/test_webui_account_features.py -k "pickup_address_is_available or pickup_address_secret" -q

Expected: FAIL because the compact row currently contains pickup_address, and the secret helper does not accept pickup_address.

- [ ] Step 3: 检查测试隔离。

Run: git diff -- tests/test_webui_account_features.py

Expected: 只有新增 API 契约测试；不使用真实邮箱地址、账号密码或验证码。

### Task 2: 实现通用 API 取件地址按需读取的后端契约

Files:
- Modify: webui/app.py
- Modify: tests/test_webui_account_features.py

Interfaces:
- Consumes: db.get_generic_api_email_by_email(email) 返回的 code_url。
- Produces: pickup_address_available: bool；field=pickup_address 的按需 secret 响应。

- [ ] Step 1: 在 webui/app.py 增加只处理 generic_api 的辅助函数。

~~~python
def _generic_api_pickup_address(row: dict) -> str:
    if str(row.get("email_source") or "").strip().lower() != "generic_api":
        return ""
    saved = str(row.get("pickup_address") or "").strip()
    if saved:
        return saved
    pool_row = db.get_generic_api_email_by_email(str(row.get("email") or "").strip())
    return str((pool_row or {}).get("code_url") or "").strip()
~~~

- [ ] Step 2: 修改 _compact_account_for_list() 和 _account_secret_value()。

列表只写入 bool(row_address)，不再写入完整 pickup_address 或 code_url。secret helper 增加 field == "pickup_address" 分支并调用同一个辅助函数；未知字段仍抛出 ValueError。

- [ ] Step 3: 运行后端契约测试。

Run: python -m pytest tests/test_webui_account_features.py -k "pickup_address_is_available or pickup_address_secret" -q

Expected: PASS；非 generic_api 账号的取件地址为空，完整地址不出现在列表响应。

- [ ] Step 4: 提交后端改动。

~~~bash
git add webui/app.py tests/test_webui_account_features.py
git commit -m "feat: protect generic api pickup address in account list"
~~~

### Task 3: 完成账号页密码纵向布局与复制按钮

Files:
- Modify: webui/templates/index.html
- Modify: tests/test_webui_account_features.py

Interfaces:
- Consumes: /api/accounts/<id>/secret?field=registration_password。
- Produces: “已设置”、显示/隐藏和复制密码纵向操作。

- [ ] Step 1: 增加前端结构契约测试。

~~~python
def test_account_template_has_vertical_password_actions_and_copy_secret(self):
    template = (
        Path(__file__).resolve().parents[1]
        / "webui" / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    self.assertIn("flex-direction: column", template)
    self.assertIn("data-account-password-toggle", template)
    self.assertIn('data-account-copy-secret="registration_password"', template)
    self.assertIn("复制密码", template)
~~~

- [ ] Step 2: 运行测试确认当前实现失败。

Run: python -m pytest tests/test_webui_account_features.py -k vertical_password_actions -q

Expected: FAIL because the current password cell is horizontal and has no copy-password action.

- [ ] Step 3: 改造 _registrationPasswordCell()。

将 .account-password-cell 改为 flex-direction: column、align-items: flex-start。密码已保存时按顺序生成“已设置”、掩码密码、“显示密码”和“复制密码”四个纵向节点；密码未知时不生成后两个按钮。

~~~html
<div class="account-password-cell">
  <span class="pill status-success">已设置</span>
  <code class="account-password-value">••••••••</code>
  <button data-account-password-toggle="ID">显示密码</button>
  <button data-account-copy-secret="registration_password"
          data-account-id="ID">复制密码</button>
</div>
~~~

- [ ] Step 4: 修改 onAccountsBodyClick()。

复制密码复用 fetchOneAccountSecret(id, "registration_password") 和 copyText(value)，请求期间禁用按钮；成功提示“密码已复制”，不把明文写入 ACCOUNTS 或其他全局状态。

- [ ] Step 5: 运行账号功能测试。

Run: python -m pytest tests/test_webui_account_features.py -q

Expected: PASS。

- [ ] Step 6: 提交密码操作改动。

~~~bash
git add webui/templates/index.html tests/test_webui_account_features.py
git commit -m "feat: add vertical account password actions"
~~~

### Task 4: 只显示“复制取件地址”并验证账号选择计数

Files:
- Modify: webui/templates/index.html
- Modify: tests/test_webui_account_features.py

Interfaces:
- Consumes: pickup_address_available 和 field=pickup_address secret。
- Produces: 不展示地址文本的复制按钮；跨分页选择数量与 ACCOUNT_SELECTED.size 一致。

- [ ] Step 1: 增加前端回归断言。

~~~python
def test_account_template_uses_lazy_pickup_copy_without_rendering_full_address(self):
    template = (
        Path(__file__).resolve().parents[1]
        / "webui" / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    self.assertIn("pickup_address_available", template)
    self.assertIn("data-account-copy-pickup-address", template)
    self.assertIn("复制取件地址", template)
    self.assertIn("field === 'pickup_address'", template)

def test_account_template_keeps_selected_count_as_single_source(self):
    template = (
        Path(__file__).resolve().parents[1]
        / "webui" / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    self.assertIn('id="accountsSelectedHintV2"', template)
    self.assertIn("ACCOUNT_SELECTED.size", template)
    self.assertIn("syncAccountsSelectAll", template)
~~~

- [ ] Step 2: 运行测试确认当前 UI 仍直接渲染地址。

Run: python -m pytest tests/test_webui_account_features.py -k "lazy_pickup_copy or selected_count" -q

Expected: FAIL until renderAccounts() stops using r.pickup_address.

- [ ] Step 3: 修改 renderAccounts()。

当 pickup_address_available 为 true 时只渲染“复制取件地址”按钮，否则显示“未设置取件地址”；不把地址放入 title、文本节点或 data 属性。

- [ ] Step 4: 修改复制事件。

按钮点击时调用 fetchOneAccountSecret(id, "pickup_address") 后复制；请求期间禁用按钮，finally 恢复；地址读取失败显示“该账号没有取件地址”或通用错误。

- [ ] Step 5: 验证选择数量。

确认 onAccountsBodyChange() 只更新 ACCOUNT_SELECTED；syncAccountsSelectAll() 只处理当前 ACCOUNTS 页面 ID；刷新和分页后仍按 ID 恢复勾选状态，批量按钮继续由 updateAccountSelectionUi() 控制。

- [ ] Step 6: 运行回归测试并提交。

Run: python -m pytest tests/test_webui_account_features.py tests/test_account_list_query.py -q

~~~bash
git add webui/templates/index.html tests/test_webui_account_features.py tests/test_account_list_query.py
git commit -m "feat: copy pickup address without rendering it"
~~~

### Task 5: 保存注册 IP 并在套餐栏展示

Files:
- Create: core/registration_network.py
- Modify: core/account_export.py
- Modify: core/db.py
- Modify: main.py
- Modify: core/browser_use_registration.py
- Modify: core/roxy_registration.py
- Modify: core/cloakbrowser_registration.py
- Modify: webui/app.py
- Modify: webui/templates/index.html
- Modify: tests/test_webui_account_features.py

Interfaces:
- Consumes: BrowserSession.exit_geo 或浏览器会话当前出口 IP。
- Produces: 可选账号字段 registration_ip，并在套餐栏“出口地区”下方展示。

- [ ] Step 1: 先写持久化与模板失败测试。

~~~python
@patch("webui.app.db.list_accounts_page")
def test_account_list_exposes_registration_ip(self, list_page):
    list_page.return_value = {
        "items": [{
            "id": 12,
            "email": "ip@example.com",
            "registration_ip": "203.0.113.10",
        }],
        "total": 1,
        "sources": [],
        "revision": "1:now",
    }
    response = self.client.get("/api/accounts?paged=1&page=1&page_size=20")
    self.assertEqual(
        response.get_json()["items"][0]["registration_ip"],
        "203.0.113.10",
    )

def test_account_template_places_registration_ip_under_exit_region(self):
    template = (
        Path(__file__).resolve().parents[1]
        / "webui" / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    self.assertIn("registration_ip", template)
    self.assertIn("注册 IP", template)
~~~

- [ ] Step 2: 运行测试确认字段尚未贯通。

Run: python -m pytest tests/test_webui_account_features.py -k registration_ip -q

Expected: FAIL because _compact_account_for_list() currently drops registration_ip.

- [ ] Step 3: 创建 core/registration_network.py。

定义 normalize_public_ip(value: object) -> str 和 extract_public_ip(payload: object) -> str。使用 ipaddress.ip_address() 校验 IPv4/IPv6；解析异常返回空字符串，不记录完整 IP 到日志。浏览器驱动使用现有配置中的 GeoIP endpoint，不新增代理凭证。

- [ ] Step 4: 打通账号保存参数。

给 save_account_data()、_append_batch_archive() 和 db.insert_account() 增加 registration_ip: str | None = None。该字段写入账号记录和批次归档；传入空值时保留已有值，兼容历史账号。

- [ ] Step 5: 在各注册驱动采集出口 IP。

- main.py 使用 session.exit_geo.get("ip")。
- core/browser_use_registration.py 在当前 Playwright page/context 上调用出口 IP helper。
- core/roxy_registration.py 和 core/cloakbrowser_registration.py 在保存前通过当前 driver 获取出口 IP。

所有驱动都把探测异常转换为空字符串后继续保存账号，不因 IP 探测失败而注册失败。

- [ ] Step 6: 返回列表字段并渲染套餐栏。

在 _compact_account_for_list() 的可选字段中加入 registration_ip；在 _planRegionDetail() 的出口地区 div 后追加“注册 IP: value”，并使用 esc() 转义。缺失字段时不渲染空行；不要用 plan_exit_ip 替代 registration_ip。

- [ ] Step 7: 运行测试。

Run: python -m pytest tests/test_webui_account_features.py tests/test_account_list_query.py -q

Expected: PASS；历史账号没有 registration_ip 时页面正常显示。

- [ ] Step 8: 提交注册 IP 改动。

~~~bash
git add core/registration_network.py core/account_export.py core/db.py main.py core/browser_use_registration.py core/roxy_registration.py core/cloakbrowser_registration.py webui/app.py webui/templates/index.html tests/test_webui_account_features.py
git commit -m "feat: persist registration exit ip"
~~~

### Task 6: 隔离注册 OTP 与设置密码 OTP

Files:
- Modify: core/email_provider.py
- Modify: core/generic_api_mail_client.py
- Modify: core/roxy_registration.py
- Create: tests/test_generic_api_otp_freshness.py
- Modify: tests/test_roxy_password_setup.py

Interfaces:
- Consumes: 注册流程内存中的 previous_otp 和 exclude_codes 参数。
- Produces: 设置密码阶段只提交非注册 OTP 的新验证码。

- [ ] Step 1: 写通用 API 旧码过滤测试。

创建 tests/test_generic_api_otp_freshness.py，使用 mock HTTP 响应，不访问真实邮箱：

~~~python
def test_generic_api_skips_excluded_code_until_new_code_arrives():
    from unittest.mock import patch
    from core import generic_api_mail_client as client

    responses = [
        FakeResponse(text="Your code is 123456"),
        FakeResponse(text="Your code is 654321"),
    ]
    account = client.GenericApiEmailAccount(
        email="user@example.com",
        code_url="https://mail.example/code",
    )
    with patch.object(client, "get_account_context", return_value=account), \
         patch.object(client.requests.Session, "get", side_effect=responses), \
         patch.object(client.time, "sleep"):
        result = client.fetch_latest_otp(
            "user@example.com",
            after_ts=0,
            max_wait=5,
            poll_interval=1,
            settle_seconds=0,
            exclude_codes={"123456"},
        )
    assert result == "654321"
~~~

测试文件内定义 FakeResponse，包含 status_code=200 和 text 属性；不打印响应内容。

- [ ] Step 2: 运行测试确认参数尚未支持。

Run: python -m pytest tests/test_generic_api_otp_freshness.py -q

Expected: FAIL because fetch_latest_otp() 和 wait_for_otp() 当前没有 exclude_codes 参数。

- [ ] Step 3: 扩展邮箱提供方接口。

将 wait_for_otp() 扩展为：

~~~python
def wait_for_otp(
    email: str,
    after_ts: float,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
    exclude_codes: set[str] | None = None,
) -> str:
~~~

仅在实际来源为 generic_api 时向 generic_api_mail_client.fetch_latest_otp() 传递 exclude_codes；其他来源保持原有时间戳过滤和调用兼容性。

- [ ] Step 4: 在通用 API 客户端过滤旧码。

在结构化响应、YangYang 列表和纯文本响应提取出 6 位 code 后统一执行：

~~~python
excluded = {
    str(code).strip()
    for code in (exclude_codes or set())
    if str(code).strip()
}
if code in excluded:
    last_error = "候选验证码属于当前流程已使用的旧验证码"
    continue
~~~

不得记录实际验证码内容。

- [ ] Step 5: 将注册 OTP 传入 Roxy 设置密码流程。

给 _run_roxy_password_setup() 和 _run_password_setup_with_gate() 增加 previous_otp: str | None = None。注册 OTP 循环成功后使用：

~~~python
openai_password = _run_password_setup_with_gate(
    driver, email, previous_otp=current_otp
)
~~~

设置密码流程进入 OTP 页后传入 exclude_codes；存在 previous_otp 时优先尝试一次页面“重新发送验证码”。只有得到非排除验证码后，才调用 _clear_otp_inputs()、_type_otp() 和提交按钮。独立设置密码任务不传 previous_otp，保持原有行为。

- [ ] Step 6: 写 Roxy 行为测试。

在 tests/test_roxy_password_setup.py mock wait_for_otp 先返回注册旧码、再返回新码，断言 _type_otp() 只收到新码且 resend 被调用；再增加 previous_otp 为空时直接使用首个有效验证码的测试。

- [ ] Step 7: 运行 OTP 测试。

Run: python -m pytest tests/test_generic_api_otp_freshness.py tests/test_roxy_password_setup.py -q

Expected: PASS；旧验证码不会进入 _type_otp()。

- [ ] Step 8: 提交 OTP 改动。

~~~bash
git add core/email_provider.py core/generic_api_mail_client.py core/roxy_registration.py tests/test_generic_api_otp_freshness.py tests/test_roxy_password_setup.py
git commit -m "fix: prevent registration otp reuse during password setup"
~~~

### Task 7: 全量验证与安全回归

Files:
- Test: tests/test_webui_account_features.py
- Test: tests/test_account_list_query.py
- Test: tests/test_generic_api_otp_freshness.py
- Test: tests/test_roxy_password_setup.py
- Test: tests/test_email_provider_gptmail.py
- Test: tests/test_generic_api_yangyang.py

Interfaces:
- Consumes: Tasks 1–6 的后端、前端和注册流程改动。
- Produces: 全量测试结果和敏感字段检查结果。

- [ ] Step 1: 运行定向回归测试。

Run: python -m pytest tests/test_webui_account_features.py tests/test_account_list_query.py tests/test_generic_api_otp_freshness.py tests/test_roxy_password_setup.py -q

Expected: PASS。

- [ ] Step 2: 运行完整测试套件。

Run: python -m pytest -q

Expected: 全部现有测试和新增测试通过；不修改真实账号、Token、邮箱池或日志数据文件。

- [ ] Step 3: 做敏感字段静态检查。

Run: rg -n "pickup_address|code_url|registration_password|registration_otp|exclude_codes" webui core tests --glob '*.py' --glob '*.html'

检查：完整 pickup_address 只出现在 secret 读取和复制链路；registration_otp 只作为局部变量或参数传递；registration_password 仍只按需读取。

- [ ] Step 4: 检查工作区和差异。

Run: git diff --check; git status --short

Expected: 无空白错误；只包含本次功能、测试和计划文档的预期变更。
