# -*- coding: utf-8 -*-
import base64
import json
import unittest
from unittest.mock import patch

from core.generic_api_mail_client import (
    GenericApiEmailAccount,
    GenericApiMailError,
    _decode_data_uri,
    _extract_code,
    _extract_structured_api_code,
    _fetch_yangyang_otp,
    _html_to_visible_text,
    _parse_yangyang_code_url,
    fetch_latest_otp,
)


class FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        return self._data


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "/api/messages/" in url:
            return FakeResponse(data={
                "items": [
                    {"id": 1, "subject": "旧码", "received_at": "2026-08-01 10:00:00"},
                    {"id": 2, "subject": "Your OpenAI code is 654321", "received_at": "2026-08-01 10:01:00"},
                ],
                "has_more": False,
            })
        if "/message/2/" in url:
            html = "<html><body>Your verification code is <b>654321</b></body></html>"
            body = "data:text/html;charset=utf-8;base64," + base64.b64encode(html.encode()).decode()
            return FakeResponse(data={"subject": "Your OpenAI code is 654321", "body": body, "receivedAt": "2026-08-01 10:01:00"})
        if "/message/1/" in url:
            return FakeResponse(data={"subject": "旧码", "body": "code 111111", "receivedAt": "2026-08-01 10:00:00"})
        return FakeResponse(status_code=404, text="not found")


class FakeInlineSession:
    def get(self, url, **kwargs):
        if "/api/messages/" in url:
            return FakeResponse(status_code=404, text="Not Found")
        return FakeResponse(text="""
        <article class="mail-card">
          <details open>
            <summary>
              <span class="subject">Your temporary ChatGPT verification code</span>
              <span class="date">2026-08-02 13:18:53</span>
            </summary>
            <div class="meta">发件人：otp@example.com</div>
            <pre class="body">Enter this temporary verification code to continue:

541409

Please ignore this email.</pre>
          </details>
        </article>
        """)


class FakeChangingCodeSession:
    def __init__(self):
        self.calls = 0

    def get(self, _url, **_kwargs):
        self.calls += 1
        code = "111111" if self.calls == 1 else "222222"
        return FakeResponse(text=f"Your OpenAI verification code is {code}")


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


class GenericApiYangyangTests(unittest.TestCase):
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

    def test_parse_yangyang_url(self):
        self.assertEqual(
            _parse_yangyang_code_url("http://yangyang.website/messages/tok/a@icloud.com"),
            ("http://yangyang.website", "tok", "a@icloud.com"),
        )

    def test_decode_data_uri_base64(self):
        body = "data:text/html;base64," + base64.b64encode("验证码 123456".encode()).decode()
        self.assertIn("123456", _decode_data_uri(body))

    def test_fetch_yangyang_otp_uses_api_and_detail(self):
        result = _fetch_yangyang_otp(
            FakeSession(),
            "http://yangyang.website/messages/tok/a@icloud.com",
            {"User-Agent": "test"},
        )
        code, meta = result
        self.assertEqual(code, "654321")
        self.assertEqual(meta["mail_id"], 2)

    def test_fetch_yangyang_otp_respects_after_ts(self):
        import datetime
        after = datetime.datetime(2026, 8, 1, 10, 2, 0).timestamp()
        result = _fetch_yangyang_otp(
            FakeSession(),
            "http://yangyang.website/messages/tok/a@icloud.com",
            {"User-Agent": "test"},
            after_ts=after,
        )
        self.assertIsNone(result)

    def test_fetch_inline_messages_page_without_api(self):
        result = _fetch_yangyang_otp(
            FakeInlineSession(),
            "https://mail.ai1998.xyz/messages/tok/cookies-benzene.48%40icloud.com",
            {"User-Agent": "test"},
        )
        code, meta = result
        self.assertEqual(code, "541409")
        self.assertEqual(meta["mail_id"], "inline-0")

    def test_fetch_latest_otp_skips_excluded_plain_response_code(self):
        session = FakeChangingCodeSession()
        account = GenericApiEmailAccount(
            email="user@example.com",
            code_url="https://mail.example/code",
        )
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session", return_value=session
        ), patch("core.generic_api_mail_client.time.sleep"):
            code = fetch_latest_otp(
                account.email,
                max_wait=2,
                poll_interval=1,
                settle_seconds=0,
                exclude_codes={"111111"},
            )

        self.assertEqual(code, "222222")
        self.assertEqual(session.calls, 2)

    def test_fetch_latest_otp_times_out_when_yangyang_only_returns_excluded_code(self):
        account = GenericApiEmailAccount(
            email="user@example.com",
            code_url="https://yangyang.website/messages/tok/user@example.com",
        )
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client._fetch_yangyang_otp",
            return_value=("111111", {"mail_id": 1, "received_at": "now"}),
        ), patch("core.generic_api_mail_client.time.sleep"):
            with self.assertRaises(GenericApiMailError):
                fetch_latest_otp(
                    account.email,
                    max_wait=0.01,
                    poll_interval=1,
                    settle_seconds=0,
                    exclude_codes={"111111"},
                )


if __name__ == "__main__":
    unittest.main()
