# 废号批量归档 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with checkpoints.

**Goal:** 将原本可能物理删除账号的批量操作改为可恢复的废号批量归档，并确保归档账号默认不显示、不进入任何后台任务。

**Architecture:** 复用当前 JSON 账号存储和已有 `archived`、`archived_at`、归档/恢复接口。新增服务端废号候选判定和原子归档接口，前端先预览候选，再二次确认；后端在真正归档时重新读取状态，避免代理错误或状态变化导致误归档。

**Tech Stack:** Python 3、现有 JSON 存储、Flask、现有账号列表分页接口、原生 JavaScript、pytest。

## Global Constraints

- 普通批量操作不再物理删除注册账号记录。
- 只把明确的 `deactivated` 账号视为废号候选。
- `failed`、`ProxyError`、403、SOCKS5 错误、超时和套餐查询失败都不是废号。
- 归档只修改本地账号记录的归档状态，不删除邮箱池、Token、Roxy 环境或 OpenAI 账号。
- 归档前后都要支持恢复，原有账号资料和查活记录保持不变。
- 归档中的账号不得新建后台任务，已经运行的任务不强行终止，由归档接口跳过并返回原因。

---

### Task 1: 固化归档字段和废号候选规则

**Files:**
- Modify: `core/db.py`
- Test: `tests/test_codex_dead_account_detection.py`
- Test: `tests/test_account_list_query.py`

**Interfaces:**
- Produces `is_dead_account_candidate(row: dict) -> bool`。
- Produces `list_dead_account_candidates(account_ids: list[int] | None = None) -> list[dict]`。
- Existing `archive_account` and `archive_accounts` remain compatible。

- [ ] **Step 1: Write failing candidate tests**

```python
def test_deactivated_live_status_is_dead_candidate():
    assert is_dead_account_candidate({"live_check_status": "deactivated"}) is True

def test_deactivated_codex_status_is_dead_candidate():
    assert is_dead_account_candidate({"codex_status": "deactivated"}) is True

def test_proxy_failure_is_not_dead_candidate():
    assert is_dead_account_candidate({
        "live_check_status": "failed",
        "live_check_error": "ProxyError: Received invalid version in initial SOCKS5 response",
    }) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_codex_dead_account_detection.py tests/test_account_list_query.py -q`

Expected: FAIL because the shared candidate helper does not exist。

- [ ] **Step 3: Implement the conservative predicate**

候选条件固定为：

```python
def is_dead_account_candidate(row: dict) -> bool:
    return (
        str(row.get("live_check_status") or "").lower() == "deactivated"
        or str(row.get("codex_status") or "").lower() == "deactivated"
    )
```

不根据错误文本猜测废号。

- [ ] **Step 4: Extend archive metadata**

归档时增加可选字段：

```python
row["archived"] = True
row["archived_at"] = now
row["archived_reason"] = "dead_account_bulk"
row["archived_source"] = "live_or_codex_deactivated"
```

取消归档只清除 `archived`、`archived_at`、`archived_reason`、`archived_source`，不清除废号检测结果。

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_codex_dead_account_detection.py tests/test_account_list_query.py -q`

Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add core/db.py tests/test_codex_dead_account_detection.py tests/test_account_list_query.py
git commit -m "feat: define conservative dead account archive candidates"
```

### Task 2: 新增预览和原子批量归档接口

**Files:**
- Modify: `core/db.py`
- Modify: `webui/app.py`
- Test: `tests/test_webui_account_features.py`

**Interfaces:**
- `GET /api/accounts/archive-dead/preview` 返回候选账号和数量。
- `POST /api/accounts/archive-dead-bulk` 接受 `{account_ids: [...]}`，返回 `archived`、`archived_count`、`skipped`。
- 新增 `db.archive_dead_accounts(account_ids, reason) -> tuple[list[dict], list[dict]]`。

- [ ] **Step 1: Write failing API tests**

```python
def test_dead_archive_preview_only_returns_deactivated_accounts(client):
    response = client.get("/api/accounts/archive-dead/preview")
    payload = response.get_json()
    assert payload["count"] == 1
    assert payload["items"][0]["id"] == 10

def test_dead_archive_bulk_revalidates_status(client):
    # 预览后把账号状态改为 failed，再提交原 ID，服务端必须 skipped。
    response = client.post(
        "/api/accounts/archive-dead-bulk",
        json={"account_ids": [10]},
    )
    assert response.get_json()["archived_count"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_webui_account_features.py -q`

Expected: FAIL because the preview and dead-account bulk endpoints do not exist。

- [ ] **Step 3: Implement the read-only preview**

预览默认只返回未归档、明确 `deactivated`、没有正在运行任务的账号，返回字段只包含：

```json
{
  "id": 10,
  "email": "masked@example.com",
  "reason": "live_check_status=deactivated",
  "live_checked_at": "2026-08-13T12:00:00"
}
```

不返回 access token、邮箱密码或完整账号复制行。

- [ ] **Step 4: Implement atomic revalidation and archive**

在 `_LOCK` 内重新读取所有传入 ID：

```python
if row.get("archived"):
    skipped.append({"id": row_id, "reason": "账号已经归档"})
elif not is_dead_account_candidate(row):
    skipped.append({"id": row_id, "reason": "当前状态已不是明确废号"})
elif has_running_account_task(row):
    skipped.append({"id": row_id, "reason": "账号仍有任务执行中"})
else:
    archive_row(row, reason="dead_account_bulk")
```

- [ ] **Step 5: Add the Flask routes**

接口必须限制单次最多 5000 个 ID，并返回明确的 `skipped` 原因。空 ID、非法 ID 和不存在的 ID 不得导致整个请求删除或归档其他账号。

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_webui_account_features.py -q`

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add core/db.py webui/app.py tests/test_webui_account_features.py
git commit -m "feat: add dead account archive preview and bulk endpoint"
```

### Task 3: 阻止归档账号进入后台任务

**Files:**
- Modify: `core/db.py`
- Modify: `webui/app.py`
- Test: `tests/test_webui_account_features.py`

**Interfaces:**
- `has_running_account_task(row: dict) -> bool`。
- 账号任务 claim 方法对 `archived=True` 返回 `False`。

- [ ] **Step 1: Write failing task guard tests**

```python
def test_archived_account_cannot_claim_password_setup():
    assert db.claim_account_password_setup(10) is False

def test_archived_account_cannot_claim_live_check():
    assert db.claim_account_live_check(10) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_webui_account_features.py -q`

Expected: FAIL because现有 claim 方法只检查 ID 和任务状态。

- [ ] **Step 3: Add the shared active-task predicate**

以下状态任意为 `queued` 或 `running` 时视为正在执行：

```python
(
    "password_setup_status",
    "live_check_status",
    "plan_check_status",
    "extract_link_status",
    "codex_agent_status",
)
```

此外 `codex_status == "retrying"` 也视为活动任务。

- [ ] **Step 4: Guard all claim methods**

在 `claim_account_password_setup`、`claim_account_live_check`、`claim_account_plan_check`、`claim_account_extract`、`claim_account_codex_agent` 找到账号后立即加入：

```python
if bool(row.get("archived")):
    return False
```

WebUI 对 `False` 返回“账号已归档，不能提交任务”，而不是显示通用失败。

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_webui_account_features.py tests/test_password_setup_task_service.py -q`

Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add core/db.py webui/app.py tests/test_webui_account_features.py
git commit -m "fix: exclude archived accounts from background tasks"
```

### Task 4: 把前端“批量删除”改为“批量归档废号”

**Files:**
- Modify: `webui/templates/index.html`
- Test: `tests/test_webui_account_features.py`

**Interfaces:**
- Existing general archive/recovery button remains usable。
- New button ID: `btnArchiveDeadAccountsV2`。
- Existing `btnDeleteSelectedAccountsV2` no longer作为普通账号批量删除入口。

- [ ] **Step 1: Write failing UI behavior tests**

```python
def test_accounts_page_contains_dead_archive_action(client):
    html = client.get("/").get_data(as_text=True)
    assert "一键归档废号" in html
    assert "/api/accounts/archive-dead/preview" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_webui_account_features.py -q`

Expected: FAIL because current页面仍显示物理删除按钮。

- [ ] **Step 3: Replace the destructive toolbar action**

将账号批量工具栏中的“删除”改为：

```html
<button id="btnArchiveDeadAccountsV2" class="jobs-tb-btn jobs-tb-btn--danger">
  一键归档废号
</button>
```

按钮只处理服务端候选，不直接使用当前勾选 ID。

- [ ] **Step 4: Implement preview confirmation flow**

前端流程：

```javascript
const preview = await api('/api/accounts/archive-dead/preview');
if (!preview.count) {
  showToast('没有可归档的明确废号');
  return;
}
const ok = confirm(`发现 ${preview.count} 个明确废号，是否归档？`);
if (!ok) return;
await api('/api/accounts/archive-dead-bulk', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({account_ids: preview.items.map(item => item.id)}),
});
```

提交结果必须显示成功数量和跳过数量，并刷新主列表及归档列表。

- [ ] **Step 5: Keep manual archive/recovery**

保留单账号“归档/恢复”和已归档账号的“恢复”按钮；“查看归档”继续使用现有 `archived=only` 查询。

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_webui_account_features.py tests/test_account_list_query.py -q`

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add webui/templates/index.html tests/test_webui_account_features.py
git commit -m "feat: replace bulk account deletion with dead-account archive"
```

### Task 5: 验证恢复、兼容和数据安全

**Files:**
- Modify: `core/db.py` only if compatibility fix is required
- Modify: `webui/app.py` only if response wording is required
- Test: `tests/test_account_list_query.py`
- Test: `tests/test_webui_account_features.py`

- [ ] **Step 1: Test legacy rows**

创建没有 `archived` 字段的旧账号记录，确认默认列表仍显示；归档后隐藏；恢复后再次显示。

- [ ] **Step 2: Test data preservation**

归档前后确认 access token、registration password、TOTP、套餐地区、查活错误和 Codex 状态内容不变。

- [ ] **Step 3: Test false-positive protection**

确认以下账号不会进入候选：

```python
{"live_check_status": "failed", "live_check_error": "ProxyError"}
{"live_check_status": "failed", "live_check_error": "403"}
{"plan_check_status": "failed"}
{"live_check_status": "unknown"}
```

- [ ] **Step 4: Run complete test suite**

Run: `pytest -q`

Expected: PASS。

- [ ] **Step 5: Commit verification changes**

```bash
git add core/db.py webui/app.py webui/templates/index.html tests
git commit -m "test: verify reversible dead-account archiving"
```

## Final User-visible Behavior

- 主账号列表默认只显示未归档账号。
- “一键归档废号”只处理明确 `deactivated` 的账号。
- 代理错误和查询失败不会被归档。
- 归档不会删除本地数据，账号可在“查看归档”中恢复。
- 归档账号不会再进入查活、查套餐、设置密码、提链或 Agent 任务。
- 批量操作返回成功数、跳过数和每个跳过原因。

