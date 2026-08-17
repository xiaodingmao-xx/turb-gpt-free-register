# Generic API OTP Extraction Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Prevent CSS/template digits such as #000000 from being submitted as email OTPs while preserving trusted JSON, plain-text, and visible-HTML extraction.

**Architecture:** Split extraction into explicit structured fields and user-visible message text. Sanitize HTML before digit matching, remove uncontextualized six-digit fallbacks, and feed verification-page rejections back into later registration polls.

**Tech Stack:** Python 3.11, standard-library json/re/html, requests, unittest tests executed through pytest.

## Global Constraints

- Do not add a special-case blacklist for 000000 or repeated digits.
- Never scan raw HTML, CSS, scripts, tag attributes, or flattened JSON metadata for fallback OTP candidates.
- Preserve exact six-digit JSON fields and exact six-digit plain-text responses.
- Do not change non-generic email provider behavior or add dependencies.
- Preserve unrelated dirty-worktree changes; stage only task-owned hunks.
- Never log full HTML, pickup URL secrets, or API tokens.

---

## File Map

- Modify core/generic_api_mail_client.py: visible-text sanitization, strict extraction, structured envelope handling, and accurate source logging.
- Modify tests/test_generic_api_yangyang.py: extraction and polling regressions.
- Modify core/roxy_registration.py: registration-local rejected-code tracking.
- Modify tests/test_roxy_password_setup.py: registration retry regression.

### Task 1: Strict Visible-Text and Structured-Envelope Extraction

**Files:**
- Modify: core/generic_api_mail_client.py:29-123
- Modify: core/generic_api_mail_client.py:241-302
- Test: tests/test_generic_api_yangyang.py

**Interfaces:**
- Produces: _html_to_visible_text(text: str) -> str
- Produces: _extract_code(text: str, *, is_html: bool | None = None) -> str | None
- Produces: _extract_structured_api_code(text: str, after_ts: float | None = None) -> tuple[str, dict] | None
- Metadata source values: json_code_field, html_visible_text, plain_text.

- [ ] **Step 1: Write failing extraction tests**

Add import json and import the three interfaces above. Add these tests to GenericApiYangyangTests:

~~~python
def test_html_visible_text_removes_nonvisible_digits(self):
    source = """
    <html><head><style>.x { color: #111111; }</style></head>
    <body data-build="222222">
      <script>const tracking = 333333;</script>
      <p>Hello, no login code is present.</p>
    </body></html>
    """
    visible = _html_to_visible_text(source)
    self.assertIn("Hello, no login code is present.", visible)
    self.assertNotRegex(visible, r"111111|222222|333333")

def test_structured_message_does_not_treat_css_black_as_otp(self):
    payload = json.dumps({
        "email": "user@example.com",
        "found": True,
        "message": '<p style="color: #000000">New sign-in to your account</p>',
        "ok": True,
    })
    self.assertIsNone(_extract_structured_api_code(payload))

def test_structured_html_prefers_visible_otp_over_css(self):
    payload = json.dumps({
        "email": "user@example.com",
        "found": True,
        "message": (
            '<div style="color: #000000">'
            "Enter this temporary verification code to continue: "
            "<strong>992669</strong></div>"
        ),
        "ok": True,
    })
    code, meta = _extract_structured_api_code(payload)
    self.assertEqual(code, "992669")
    self.assertEqual(meta["source"], "html_visible_text")

def test_structured_envelope_respects_false_status(self):
    for key in ("ok", "found"):
        payload = {
            "email": "user@example.com",
            "found": True,
            "message": "Your verification code is 992669",
            "ok": True,
        }
        payload[key] = False
        with self.subTest(key=key):
            self.assertIsNone(_extract_structured_api_code(json.dumps(payload)))

def test_explicit_json_code_requires_exact_six_digits(self):
    valid = _extract_structured_api_code(json.dumps({"code": "992669"}))
    invalid = _extract_structured_api_code(
        json.dumps({"code": "prefix-992669-suffix"})
    )
    self.assertEqual(valid[0], "992669")
    self.assertEqual(valid[1]["source"], "json_code_field")
    self.assertIsNone(invalid)

def test_plain_text_requires_exact_value_or_context(self):
    self.assertEqual(_extract_code("992669"), "992669")
    self.assertEqual(
        _extract_code("Your verification code is 992669"),
        "992669",
    )
    self.assertIsNone(_extract_code("build identifier 992669"))
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~powershell
py -3.11 -m pytest tests/test_generic_api_yangyang.py -k "visible_text or css_black or visible_otp or false_status or explicit_json or plain_text" -v
~~~

Expected: failures because the sanitizer is absent, CSS 000000 is returned, false envelopes are parsed, and mixed explicit fields are partially matched.

- [ ] **Step 3: Implement HTML sanitization and strict text matching**

Add an HTML marker and implement:

~~~python
_HTML_MARKER_RE = re.compile(
    r"<(?:!doctype|html|head|body|style|script|table|div|p|span|br|strong)\b",
    re.IGNORECASE,
)

def _html_to_visible_text(text: str) -> str:
    body = _decode_data_uri(text or "")
    body = re.sub(
        r"<(style|script|head)\b[^>]*>.*?</\1\s*>",
        " ",
        body,
        flags=re.DOTALL | re.IGNORECASE,
    )
    body = re.sub(r"<[^>]+>", " ", body)
    body = html_lib.unescape(body)
    return re.sub(r"\s+", " ", body).strip()

def _extract_code(text: str, *, is_html: bool | None = None) -> str | None:
    body = _decode_data_uri(text or "").strip()
    if not body:
        return None
    html_input = bool(_HTML_MARKER_RE.search(body)) if is_html is None else is_html
    searchable = _html_to_visible_text(body) if html_input else html_lib.unescape(body)
    searchable = searchable.strip()
    if re.fullmatch(r"\d{6}", searchable):
        return searchable
    lower = searchable.lower()
    for match in _CODE_REGEX.finditer(searchable):
        window = lower[
            max(0, match.start() - 80):
            min(len(lower), match.end() + 80)
        ]
        if any(word.lower() in window for word in _CONTEXT_WORDS):
            return match.group(1)
    return None
~~~

Do not retain return codes[-1]. Check whether extract_otp is used elsewhere in the module before removing its import:

~~~powershell
rg -n "extract_otp" core/generic_api_mail_client.py
~~~

- [ ] **Step 4: Implement structured fields and envelope parsing**

Use exact full matching for explicit fields. A malformed explicit field returns None rather than silently falling back.

~~~python
if data.get("ok") is False or data.get("found") is False:
    return None

raw_code = next(
    (
        data.get(name)
        for name in (
            "code", "otp", "verification_code", "verificationCode",
            "email_code", "emailCode",
        )
        if data.get(name) is not None
    ),
    None,
)
code = None
source = ""
if raw_code is not None:
    value = str(raw_code).strip()
    if re.fullmatch(r"\d{6}", value):
        code = value
        source = "json_code_field"
else:
    message = next(
        (
            data.get(name)
            for name in ("message", "text", "content", "html", "body")
            if isinstance(data.get(name), str) and data.get(name).strip()
        ),
        "",
    )
    is_html = bool(_HTML_MARKER_RE.search(_decode_data_uri(message)))
    code = _extract_code(message, is_html=is_html)
    source = "html_visible_text" if is_html else "plain_text"

if not code:
    return None
~~~

Keep existing timestamp filtering. Return source in metadata instead of the constant structured_api.

- [ ] **Step 5: Run GREEN tests**

~~~powershell
py -3.11 -m pytest tests/test_generic_api_yangyang.py -v
~~~

Expected: all existing and new tests pass.

- [ ] **Step 6: Commit Task 1**

~~~powershell
git add -- core/generic_api_mail_client.py tests/test_generic_api_yangyang.py
git diff --cached --check
git commit -m "fix: ignore HTML template digits when extracting OTP"
~~~

### Task 2: Polling Source Logs and Settle Regression

**Files:**
- Modify: core/generic_api_mail_client.py:618-676
- Test: tests/test_generic_api_yangyang.py

**Interfaces:**
- Consumes: meta["source"] from Task 1.
- Produces: CSS-only responses never populate best_otp or start settle.

- [ ] **Step 1: Add a notification-then-OTP fake session**

~~~python
class FakeNotificationThenOtpSession:
    def __init__(self):
        self.calls = 0

    def get(self, _url, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            message = '<p style="color: #000000">New sign-in</p>'
        else:
            message = (
                '<div style="color: #000000">'
                "Enter this temporary verification code to continue: 992669"
                "</div>"
            )
        return FakeResponse(text=json.dumps({
            "email": "user@example.com",
            "found": True,
            "message": message,
            "ok": True,
        }))
~~~

Add the polling test:

~~~python
def test_polling_ignores_css_until_visible_otp_arrives(self):
    session = FakeNotificationThenOtpSession()
    account = GenericApiEmailAccount(
        email="user@example.com",
        code_url="https://mail.example/code",
    )
    with patch(
        "core.generic_api_mail_client.get_account_context",
        return_value=account,
    ), patch(
        "core.generic_api_mail_client.requests.Session",
        return_value=session,
    ), patch("core.generic_api_mail_client.time.sleep"):
        code = fetch_latest_otp(
            account.email,
            max_wait=2,
            poll_interval=1,
            settle_seconds=0,
        )
    self.assertEqual(code, "992669")
    self.assertEqual(session.calls, 2)
~~~

- [ ] **Step 2: Run the polling regression**

~~~powershell
py -3.11 -m pytest tests/test_generic_api_yangyang.py::GenericApiYangyangTests::test_polling_ignores_css_until_visible_otp_arrives -v
~~~

Expected after Task 1: PASS. If it fails, locate any remaining raw-JSON extraction before changing timing code.

- [ ] **Step 3: Report precise candidate sources**

In both initial and replacement structured-candidate log branches, derive:

~~~python
source = structured_meta.get("source") or "structured_api"
~~~

Use source={source} in the log string. Do not log message or response bodies.

- [ ] **Step 4: Verify and commit Task 2**

~~~powershell
py -3.11 -m pytest tests/test_generic_api_yangyang.py -v
git add -- core/generic_api_mail_client.py tests/test_generic_api_yangyang.py
git diff --cached --check
git commit -m "test: cover delayed visible OTP polling"
~~~

### Task 3: Exclude OTPs Rejected During Registration

**Files:**
- Modify: core/roxy_registration.py:2346-2389
- Test: tests/test_roxy_password_setup.py

**Interfaces:**
- Consumes: wait_for_otp(email, after_ts, exclude_codes=None) -> str.
- Produces: rejected_codes: set[str], local to one run_roxy_registration call.

- [ ] **Step 1: Write the failing retry assertion**

Add a run_roxy_registration test using the same FakeDriver/FakeClient and dependency patches as test_password_setup_failure_still_saves_registration_as_success, with these changed patches and assertions:

~~~python
patch.object(
    service,
    "_wait_after_email_otp_submit",
    side_effect=["invalid", "accepted"],
),
patch.object(service, "_click_resend_email_otp"),
patch.object(
    service,
    "wait_for_otp",
    return_value="992669",
) as wait_otp,
patch.object(service._cfg, "ROXY_PASSWORD_SETUP_ENABLED", False),
~~~

Call:

~~~python
result = service.run_roxy_registration(
    email="user@example.com",
    name="Test User",
    birthday="2000-01-01",
    otp_code="000000",
)
~~~

Assert:

~~~python
self.assertTrue(result["success"])
wait_otp.assert_called_once()
self.assertEqual(
    wait_otp.call_args.kwargs["exclude_codes"],
    {"000000"},
)
~~~

Keep all browser/session/save mocks explicit so the test performs no network or browser action.

- [ ] **Step 2: Run the test and verify RED**

~~~powershell
py -3.11 -m pytest tests/test_roxy_password_setup.py::RoxyPasswordSetupTests::test_registration_retry_excludes_code_rejected_by_verification_page -v
~~~

Expected: FAIL because wait_for_otp currently receives only after_ts.

- [ ] **Step 3: Track rejected candidates in the registration loop**

Initialize beside current_otp:

~~~python
current_otp = otp_code
rejected_codes: set[str] = set()
max_otp_attempts = 3
~~~

Build fetch arguments without passing an empty set:

~~~python
otp_kwargs = {"after_ts": otp_after_ts}
if rejected_codes:
    otp_kwargs["exclude_codes"] = rejected_codes
current_otp = wait_for_otp(email, **otp_kwargs)
~~~

After a non-accepted outcome and before resetting current_otp:

~~~python
rejected_codes.add(str(current_otp).strip())
~~~

Do not alter _run_roxy_password_setup; its generic-API same-code resend behavior is separate.

- [ ] **Step 4: Run focused registration tests**

~~~powershell
py -3.11 -m pytest tests/test_roxy_password_setup.py -v
~~~

Expected: all tests pass.

- [ ] **Step 5: Stage only task-owned hunks and commit**

These files already have user-owned edits. Inspect and interactively stage only rejected_codes and its test:

~~~powershell
git diff -- core/roxy_registration.py tests/test_roxy_password_setup.py
git add -p -- core/roxy_registration.py tests/test_roxy_password_setup.py
git diff --cached --check
git diff --cached -- core/roxy_registration.py tests/test_roxy_password_setup.py
git commit -m "fix: avoid resubmitting rejected registration OTPs"
~~~

Do not stage pre-existing password-setup or registration metadata changes.

### Task 4: Full Regression and Sanitized Reproduction

**Files:**
- Verify: core/generic_api_mail_client.py
- Verify: core/roxy_registration.py
- Verify: tests/test_generic_api_yangyang.py
- Verify: tests/test_roxy_password_setup.py

**Interfaces:**
- Consumes all Task 1-3 behavior.
- Produces verification evidence; no new production interface.

- [ ] **Step 1: Run focused suites**

~~~powershell
py -3.11 -m pytest tests/test_generic_api_yangyang.py tests/test_roxy_password_setup.py -v
~~~

Expected: all tests pass.

- [ ] **Step 2: Run related provider and registration suites**

~~~powershell
py -3.11 -m pytest tests/test_email_provider_gptmail.py tests/test_registration_retry.py -v
~~~

Expected: all tests pass.

- [ ] **Step 3: Run the complete suite**

~~~powershell
py -3.11 -m pytest -q
~~~

Expected: exit code 0. Record any unrelated pre-existing failure with its exact test and traceback.

- [ ] **Step 4: Run a sanitized local reproduction**

~~~powershell
@'
import json
from core.generic_api_mail_client import _extract_structured_api_code

notification = json.dumps({
    "email": "redacted@example.com",
    "found": True,
    "message": '<p style="color:#000000">New sign-in</p>',
    "ok": True,
})
otp = json.dumps({
    "email": "redacted@example.com",
    "found": True,
    "message": '<div style="color:#000000">Your verification code is 992669</div>',
    "ok": True,
})
assert _extract_structured_api_code(notification) is None
assert _extract_structured_api_code(otp)[0] == "992669"
print("sanitized OTP reproduction passed")
'@ | py -3.11 -
~~~

Expected: sanitized OTP reproduction passed.

- [ ] **Step 5: Inspect final scope**

~~~powershell
git diff --check
git status --short
git log -4 --oneline
~~~

Confirm no pickup secret, full HTML, access token, or unrelated dirty-worktree change was committed. Report remaining user-owned modifications without changing them.

