# 套餐地区与支付会话识别设计

## 目标

在账号列表中补充两类地区信息，并在提链结果中显示支付结账会话类型：

- 套餐资格地区：从 `accounts/check` 响应中读取明确的国家/地区字段；没有返回时显示“未返回”。
- 查询出口地区：使用本次套餐查询实际采用的网络路径做 IP Geo 查询，记录 IP、国家、地区和城市；查询失败不影响套餐结果。
- 支付会话类型：解析提链服务返回的支付 JSON，识别 `oaics_` 会话或 Stripe `cs_` 会话，并在 WebUI 显示识别结果。

## 非目标

- 不在套餐查询中伪造或推断套餐资格国家。
- 不把代理出口地区当作套餐资格地区。
- 不发起支付、不创建 Checkout Session、不提交银行卡或支付方式。
- 不改变现有提链服务的请求参数、CDK 扣次逻辑和 SSE 事件协议。

## 设计

### 1. 套餐查询地区

`core/chatgpt_plan.py` 在解析 `accounts/check` 时，从候选对象递归读取以下字段，按优先级取第一个非空值：

国家字段：`country`、`country_code`、`countryCode`、`billing_country`、`billingCountry`。

地区字段：`region`、`region_code`、`regionCode`、`residency_region`、`residencyRegion`。

结果字段为：

- `plan_eligibility_country`
- `plan_eligibility_region`
- `plan_eligibility_region_source`，固定为 `accounts_check` 或空值

只读取接口明确返回的字段；没有字段时不使用代理地区回填。

### 2. 查询出口地区

`core/chatgpt_plan.py` 增加轻量的 `_detect_plan_exit_geo`，复用套餐查询建立的 HTTP 会话和实际代理，访问现有 `config.browser.IP_GEO_ENDPOINTS` 中的地理信息接口。

只保存以下非敏感派生字段：

- `plan_exit_ip`
- `plan_exit_country`
- `plan_exit_region`
- `plan_exit_city`
- `plan_exit_timezone`
- `plan_exit_geo_source`

GeoIP 调用失败、超时或返回非 JSON 时，套餐查询仍按原流程返回；上述字段为空，并把原因写入调试日志而不是套餐失败原因。代理发生直连回退时，出口地区表示最终实际使用的直连路径。

### 3. 离线支付会话识别

新增 `core/payment_method_detector.py`，采用附加代码的纯函数结构，不发起网络请求。

公开接口：

```python
parse_checkout_session(payload, *, billing_country, fallback_publishable_key="")
parse_capability_evidence(stripe_init_payload, *, fallback_currency="")
classify_payment_method(evidence, expected_method)
detect_oaics(checkout_payload, stripe_init_payload=None, *, billing_country, fallback_currency="", expected_method="paypal")
detect_extract_payment_session(extract_payload, *, billing_country="", fallback_currency="", expected_method="")
```

`detect_extract_payment_session` 会在提链返回的嵌套 JSON 中寻找带有 `oaics_` 或 `cs_` 前缀的 Checkout Session，并寻找支付能力字段；找不到 Checkout Session 时返回 `detected: false`，不抛出异常。

派生结果包含：

- `detected`
- `checkout_session_id`
- `session_kind`：`oaics` 或 `stripe_cs`
- `is_oaics`
- `processor_entity`
- `method_status`
- `method_available`
- `payment_method_types`
- `currency`
- `amount_minor`
- `offer_state`

不保存附加代码中的原始支付 JSON；项目原有的 `extract_link_result_json` 行为保持不变。

### 4. 提链结果接入

`core/extract_link_service.py` 在收到 SSE `result` 后调用离线检测器，把派生对象写入最终任务结果的 `payment_detection` 字段。检测异常只记录日志，不让已成功的提链结果变成失败。

`core/db.py` 将派生字段保存到账号记录：

- `extract_link_payment_session_id`
- `extract_link_payment_session_kind`
- `extract_link_payment_is_oaics`
- `extract_link_payment_detected`
- `extract_link_payment_method_status`
- `extract_link_payment_method_available`
- `extract_link_payment_methods`
- `extract_link_payment_currency`
- `extract_link_payment_amount_minor`
- `extract_link_payment_offer_state`

### 5. WebUI 展示

套餐列显示：

```text
free(可Plus试用)
资格地区: JP
出口地区: JP / Tokyo
```

缺失值分别显示“未返回”和“未知”，并通过 `title` 展示 IP、时区和数据来源。

提链列显示：

```text
提链成功(PIX)
会话: OAICS (oaics_...)
```

或：

```text
会话: Stripe cs_...
```

未找到会话时显示“会话: 未识别”，不影响复制链接和二维码按钮。

## 错误处理

- Access Token 过期仍按原逻辑返回“请查活刷新 Token”。
- 套餐接口 HTTP 错误仍按原重试策略处理。
- GeoIP 错误只产生空的出口地区字段和 debug 日志。
- 支付 JSON 缺失、结构未知或无合法前缀时，识别结果为 `detected: false`。
- 支付识别器只解析本地对象，不读文件、不联网、不记录 Token 或完整支付载荷。

## 测试

- `parse_accounts_check` 能提取标准地区字段，并在没有地区字段时返回空值。
- 出口 GeoIP 成功、超时和非 JSON 场景都不影响套餐成功结果。
- `detect_oaics` 正确识别 `oaics_` 与 `cs_`。
- 嵌套提链结果能被发现；不存在合法会话时返回 `detected: false`。
- DB 保存支付识别派生字段。
- WebUI 套餐列和提链列包含两类地区与会话标签。

