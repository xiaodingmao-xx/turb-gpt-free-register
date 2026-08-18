# -*- coding: utf-8 -*-
import base64
import datetime
import json
import unittest
from unittest.mock import patch

from core.generic_api_mail_client import (
    GenericApiEmailAccount,
    GenericApiMailError,
    GenericOtpObservation,
    OtpBaseline,
    _decode_data_uri,
    _extract_code,
    _extract_structured_api_code,
    _extract_yangyang_openai_code,
    _fetch_yangyang_otp,
    _html_to_visible_text,
    _otp_observation_key,
    _parse_generic_api_observation,
    _parse_yangyang_code_url,
    capture_otp_baseline,
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
    def __init__(self, subject="Your temporary ChatGPT verification code"):
        self.subject = subject

    def get(self, url, **kwargs):
        if "/api/messages/" in url:
            return FakeResponse(status_code=404, text="Not Found")
        return FakeResponse(text="""
        <article class="mail-card">
          <details open>
            <summary>
              <span class="subject">%s</span>
              <span class="date">2026-08-02 13:18:53</span>
            </summary>
            <div class="meta">发件人：otp@example.com</div>
            <pre class="body">Enter this temporary verification code to continue:

541409

Please ignore this email.</pre>
          </details>
        </article>
        """ % self.subject)


class FakeYangyangBaselineSkewSession:
    def get(self, url, **_kwargs):
        if "/api/messages/" in url:
            return FakeResponse(data={
                "items": [
                    {
                        "id": "old-id",
                        "subject": "Your OpenAI code is 119006",
                        "received_at": "2026-08-17 10:01:40",
                    },
                    {
                        "id": "new-id",
                        "subject": "Your OpenAI code is 119006",
                        "received_at": "2026-08-17 10:01:37",
                    },
                ],
                "has_more": False,
            })
        if "/message/old-id/" in url:
            return FakeResponse(data={
                "subject": "Your OpenAI code is 119006",
                "body": "Enter this temporary verification code: 119006",
                "receivedAt": "2026-08-17 10:01:40",
            })
        if "/message/new-id/" in url:
            return FakeResponse(data={
                "subject": "Your OpenAI code is 119006",
                "body": "Enter this temporary verification code: 119006",
                "receivedAt": "2026-08-17 10:01:37",
            })
        return FakeResponse(status_code=404, text="not found")


class FakeInlineBaselineSkewSession:
    def get(self, url, **_kwargs):
        if "/api/messages/" in url:
            return FakeResponse(status_code=404, text="Not Found")
        return FakeResponse(text="""
        <article class="mail-card" data-message-id="old-id">
          <span class="subject">Your OpenAI code is 119006</span>
          <span class="date">2026-08-17 10:01:40</span>
          <div class="meta">otp@example.com</div>
          <pre class="body">Enter this temporary verification code: 119006</pre>
        </article>
        <article class="mail-card" data-message-id="new-id">
          <span class="subject">Your OpenAI code is 119006</span>
          <span class="date">2026-08-17 10:01:37</span>
          <div class="meta">otp@example.com</div>
          <pre class="body">Enter this temporary verification code: 119006</pre>
        </article>
        """)


class FakeInlineInsertedMessageSession:
    def __init__(self, *, include_new: bool):
        self.include_new = include_new

    def get(self, url, **_kwargs):
        if "/api/messages/" in url:
            return FakeResponse(status_code=404, text="Not Found")
        new_card = """
        <article class="mail-card">
          <span class="subject">Your OpenAI code is 119006</span>
          <span class="date">2026-08-17 10:01:37</span>
          <div class="meta">otp@example.com</div>
          <pre class="body">Enter this temporary verification code: 119006 new delivery</pre>
        </article>
        """ if self.include_new else ""
        old_card = """
        <article class="mail-card">
          <span class="subject">Your OpenAI code is 119006</span>
          <span class="date">2026-08-17 10:01:40</span>
          <div class="meta">otp@example.com</div>
          <pre class="body">Enter this temporary verification code: 119006 old delivery</pre>
        </article>
        """
        return FakeResponse(text=new_card + old_card)


class FakeYangyangErrorSession:
    def get(self, _url, **_kwargs):
        return FakeResponse(status_code=500, text="Your verification code is 654321")


class FakeInlineErrorSession:
    def get(self, url, **_kwargs):
        if "/api/messages/" in url:
            return FakeResponse(status_code=404, text="Not Found")
        return FakeResponse(status_code=500, text="Your verification code is 654321")


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


class FakeOldStructuredCodeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get(self, _url, **_kwargs):
        self.calls += 1
        return FakeResponse(text=json.dumps(self.payload))


class FakeSingleResponseSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get(self, _url, **_kwargs):
        self.calls += 1
        return FakeResponse(text=json.dumps(self.payload))


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeSequenceSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def get(self, _url, **_kwargs):
        self.calls += 1
        payload = self.payloads[min(self.calls - 1, len(self.payloads) - 1)]
        return FakeResponse(text=json.dumps(payload))


class GenericApiYangyangTests(unittest.TestCase):
    def test_yangyang_openai_parser_rejects_noise_only_zero_code(self):
        code = _extract_yangyang_openai_code(
            "Your temporary ChatGPT login code",
            "Enter this temporary verification code to continue: 000000",
        )

        self.assertIsNone(code)

    def test_yangyang_openai_parser_ignores_zero_noise_and_keeps_real_code(self):
        code = _extract_yangyang_openai_code(
            "Your temporary ChatGPT login code",
            "Template placeholder 000000. Enter this temporary verification code: 992669",
        )

        self.assertEqual(code, "992669")

    def test_yangyang_fallback_does_not_return_zero_noise_when_real_candidate_has_no_context(self):
        code = _extract_yangyang_openai_code(
            "Login verification",
            "code: 000000 reference 992669",
        )

        self.assertIsNone(code)

    def test_yangyang_fallback_rejects_unbranded_zero_noise_only(self):
        code = _extract_yangyang_openai_code(
            "Login verification",
            "code: 000000",
        )

        self.assertIsNone(code)

    def test_structured_old_code_is_recognized_but_rejected(self):
        payload = json.dumps({
            "code": "174510",
            "time": "2026-08-17T14:40:00+08:00",
            "message_id": "mail-old",
        })
        after = datetime.datetime.fromisoformat("2026-08-17T14:53:40+08:00").timestamp()

        observation = _parse_generic_api_observation(payload, after_ts=after)

        self.assertTrue(observation.structured)
        self.assertIsNone(observation.code)
        self.assertEqual(observation.rejection_reason, "before_trigger")
        self.assertEqual(observation.message_id, "mail-old")

    def test_structured_old_code_must_not_fall_back_to_raw_regex(self):
        payload = json.dumps({
            "code": "174510",
            "time": "2026-08-17T14:40:00+08:00",
        })
        after = datetime.datetime.fromisoformat("2026-08-17T14:53:40+08:00").timestamp()

        observation = _parse_generic_api_observation(payload, after_ts=after)

        self.assertTrue(observation.structured)
        self.assertIsNone(observation.code)

    def test_structured_code_older_than_max_age_is_rejected(self):
        observation = _parse_generic_api_observation(
            json.dumps({"code": "174510", "timestamp": 100.0}),
            max_age_seconds=1000,
            now_ts=2000.0,
        )

        self.assertIsNone(observation.code)
        self.assertEqual(observation.rejection_reason, "older_than_max_age")

    def test_structured_code_without_timestamp_keeps_metadata_missing_state(self):
        observation = _parse_generic_api_observation(json.dumps({"code": "174510"}))

        self.assertEqual(observation.code, "174510")
        self.assertTrue(observation.structured)
        self.assertIsNone(observation.msg_ts)
        self.assertIsNone(observation.rejection_reason)

    def test_structured_nested_message_extracts_code_and_metadata(self):
        payload = json.dumps({
            "email": "user@example.com",
            "found": True,
            "ok": True,
            "message": {
                "cc": "",
                "code": "401873",
                "date": "Mon, 17 Aug 2026 15:47:00 +0800",
                "from": "noreply@example.com",
                "html": "<p>Enter this temporary verification code: 401873</p>",
                "subject": "Your temporary ChatGPT login code",
                "text": "Enter this temporary verification code to continue: 401873",
                "timestamp": 200.0,
                "to": "user@example.com",
                "uid": "mail-1047",
            },
        })

        observation = _parse_generic_api_observation(payload, after_ts=100.0)

        self.assertEqual(observation.code, "401873")
        self.assertEqual(observation.msg_ts, 200.0)
        self.assertEqual(observation.message_id, "mail-1047")
        self.assertEqual(observation.subject, "Your temporary ChatGPT login code")
        self.assertIsNone(observation.rejection_reason)

    def test_structured_nested_message_respects_after_ts(self):
        payload = json.dumps({
            "found": True,
            "ok": True,
            "message": {
                "code": "401873",
                "date": "Mon, 17 Aug 2026 15:40:00 +0800",
                "timestamp": 100.0,
                "uid": "mail-old",
            },
        })

        observation = _parse_generic_api_observation(payload, after_ts=200.0)

        self.assertIsNone(observation.code)
        self.assertEqual(observation.rejection_reason, "before_trigger")
        self.assertEqual(observation.message_id, "mail-old")

    def test_polling_returns_code_from_nested_message_envelope(self):
        now_ts = datetime.datetime.now().timestamp()
        account = GenericApiEmailAccount(
            "user@example.com",
            "https://mail.example/code",
        )
        session = FakeSingleResponseSession({
            "email": "user@example.com",
            "found": True,
            "ok": True,
            "message": {
                "code": "401873",
                "date": "Mon, 17 Aug 2026 15:47:00 +0800",
                "timestamp": now_ts,
                "uid": "mail-1047",
                "subject": "Your temporary ChatGPT login code",
            },
        })

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session", return_value=session
        ), patch("core.generic_api_mail_client.time.sleep"):
            code = fetch_latest_otp(
                account.email,
                after_ts=now_ts - 10,
                max_wait=0.01,
                poll_interval=1,
                settle_seconds=0,
            )

        self.assertEqual(code, "401873")

    def test_polling_does_not_fallback_to_old_structured_json_code(self):
        account = GenericApiEmailAccount(
            "user@example.com",
            "https://mail.example/code",
        )
        session = FakeOldStructuredCodeSession({
            "code": "174510",
            "time": "2026-08-17T14:40:00+08:00",
        })
        after = datetime.datetime.fromisoformat("2026-08-17T14:53:40+08:00").timestamp()

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session", return_value=session
        ), patch("core.generic_api_mail_client.time.sleep"):
            with self.assertRaises(GenericApiMailError):
                fetch_latest_otp(
                    account.email,
                    after_ts=after,
                    max_wait=0.01,
                    poll_interval=1,
                    settle_seconds=0,
                )

    def test_capture_baseline_records_cached_code_before_trigger(self):
        account = GenericApiEmailAccount(
            "user@example.com",
            "https://mail.example/code",
        )
        session = FakeSingleResponseSession({
            "code": "174510",
            "message_id": "mail-before-trigger",
        })

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session", return_value=session
        ):
            baseline = capture_otp_baseline(account.email, attempts=1)

        self.assertEqual(baseline.codes, frozenset({"174510"}))
        self.assertEqual(baseline.message_ids, frozenset({"mail-before-trigger"}))
        self.assertGreater(baseline.captured_at, 0)

    def test_capture_baseline_raises_after_request_failures(self):
        account = GenericApiEmailAccount(
            "user@example.com",
            "https://mail.example/code",
        )

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session.get",
            side_effect=RuntimeError("baseline timeout"),
        ), patch("core.generic_api_mail_client.time.sleep"):
            with self.assertRaisesRegex(GenericApiMailError, "基线"):
                capture_otp_baseline(account.email, attempts=3)

    def test_polling_waits_until_code_changes_from_baseline(self):
        account = GenericApiEmailAccount(
            "user@example.com",
            "https://mail.example/code",
        )
        session = FakeChangingCodeSession()
        baseline = OtpBaseline(frozenset({"111111"}), frozenset(), 1.0)

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session", return_value=session
        ), patch("core.generic_api_mail_client.time.sleep"):
            code = fetch_latest_otp(
                account.email,
                max_wait=2,
                poll_interval=1,
                settle_seconds=0,
                otp_baseline=baseline,
            )

        self.assertEqual(code, "222222")
        self.assertEqual(session.calls, 2)

    def test_candidate_is_confirmed_after_search_window_expires(self):
        account = GenericApiEmailAccount(
            "user@example.com",
            "https://mail.example/code",
        )
        clock = FakeClock()
        session = FakeSingleResponseSession({"code": "107902"})

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session", return_value=session
        ), patch("core.generic_api_mail_client.time.time", side_effect=clock.time), patch(
            "core.generic_api_mail_client.time.sleep", side_effect=clock.sleep
        ):
            code = fetch_latest_otp(
                account.email,
                max_wait=0.01,
                poll_interval=1,
                settle_seconds=30,
            )

        self.assertEqual(code, "107902")
        self.assertEqual(clock.now, 130.0)

    def test_same_code_with_new_message_id_is_accepted_after_baseline(self):
        account = GenericApiEmailAccount("user@example.com", "https://mail.example/code")
        session = FakeSequenceSession([{
            "code": "111111", "message_id": "mail-new",
        }])
        baseline = OtpBaseline(frozenset({"111111"}), frozenset({"mail-old"}), 1.0)

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session", return_value=session
        ), patch("core.generic_api_mail_client.time.sleep"):
            code = fetch_latest_otp(
                account.email, max_wait=1, poll_interval=1, settle_seconds=0,
                otp_baseline=baseline,
            )

        self.assertEqual(code, "111111")

    def test_same_code_with_new_message_id_survives_timestamp_skew(self):
        account = GenericApiEmailAccount("user@example.com", "https://mail.example/code")
        after_ts = datetime.datetime.now().timestamp()
        session = FakeSequenceSession([{
            "code": "111111",
            "message_id": "mail-new",
            "timestamp": after_ts - (24 * 60 * 60),
        }])
        baseline = OtpBaseline(
            frozenset({"111111"}),
            frozenset({"mail-old"}),
            after_ts - 1,
        )

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session", return_value=session
        ), patch("core.generic_api_mail_client.time.sleep"):
            code = fetch_latest_otp(
                account.email,
                after_ts=after_ts,
                max_wait=0.01,
                poll_interval=0.01,
                settle_seconds=0,
                otp_baseline=baseline,
            )

        self.assertEqual(code, "111111")

    def test_otp_observation_key_uses_empty_strings_for_missing_metadata(self):
        observation = _parse_generic_api_observation(json.dumps({"code": "111111"}))

        self.assertEqual(_otp_observation_key(observation), ("", "", "111111"))

    def test_otp_observation_key_preserves_zero_value_metadata(self):
        observation = GenericOtpObservation(
            code="111111",
            source="structured_api",
            received_at=None,
            msg_ts=0.0,
            message_id=0,
            structured=True,
        )

        self.assertEqual(_otp_observation_key(observation), ("0", "0.0", "111111"))

    def test_candidate_before_search_deadline_gets_full_settle_window(self):
        account = GenericApiEmailAccount("user@example.com", "https://mail.example/code")
        clock = FakeClock()
        session = FakeSequenceSession([{
            "code": "111111", "message_id": "mail-1", "timestamp": 101.0,
        }])

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session", return_value=session
        ), patch("core.generic_api_mail_client.time.time", side_effect=clock.time), patch(
            "core.generic_api_mail_client.time.sleep", side_effect=clock.sleep
        ):
            code = fetch_latest_otp(
                account.email, max_wait=5, poll_interval=2, settle_seconds=10,
            )

        self.assertEqual(code, "111111")
        self.assertEqual(clock.now, 110.0)
        self.assertTrue(all(seconds <= 2 for seconds in clock.sleeps))

    def test_same_code_new_message_resets_settle_window(self):
        account = GenericApiEmailAccount("user@example.com", "https://mail.example/code")
        clock = FakeClock()
        session = FakeSequenceSession([
            {"code": "111111", "message_id": "mail-1", "timestamp": 101.0},
            {"code": "111111", "message_id": "mail-1", "timestamp": 101.0},
            {"code": "111111", "message_id": "mail-2", "timestamp": 102.0},
        ])

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session", return_value=session
        ), patch("core.generic_api_mail_client.time.time", side_effect=clock.time), patch(
            "core.generic_api_mail_client.time.sleep", side_effect=clock.sleep
        ):
            code = fetch_latest_otp(
                account.email, max_wait=5, poll_interval=1, settle_seconds=3,
            )

        self.assertEqual(code, "111111")
        self.assertEqual(clock.now, 105.0)

    def test_changing_candidate_hits_hard_confirmation_limit(self):
        account = GenericApiEmailAccount("user@example.com", "https://mail.example/code")
        clock = FakeClock()
        session = FakeSequenceSession([
            {"code": "111111", "message_id": f"mail-{index}", "timestamp": float(index)}
            for index in range(20)
        ])

        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session", return_value=session
        ), patch("core.generic_api_mail_client.time.time", side_effect=clock.time), patch(
            "core.generic_api_mail_client.time.sleep", side_effect=clock.sleep
        ):
            with self.assertRaisesRegex(GenericApiMailError, "候选不稳定"):
                fetch_latest_otp(
                    account.email, max_wait=2, poll_interval=1, settle_seconds=2,
                )

        self.assertEqual(clock.now, 115.0)

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

    def test_yangyang_helper_skips_baseline_id_and_returns_new_same_code_with_skewed_time(self):
        result = _fetch_yangyang_otp(
            FakeYangyangBaselineSkewSession(),
            "http://yangyang.website/messages/tok/a@icloud.com",
            {"User-Agent": "test"},
            baseline_message_ids=frozenset({"old-id"}),
        )

        code, meta = result
        self.assertEqual(code, "119006")
        self.assertEqual(meta["mail_id"], "new-id")

    def test_yangyang_otp_log_does_not_expose_code_in_subject(self):
        with self.assertLogs("core.generic_api_mail_client", level="INFO") as logs:
            _fetch_yangyang_otp(
                FakeSession(),
                "http://yangyang.website/messages/tok/a@icloud.com",
                {"User-Agent": "test"},
            )

        self.assertNotIn("654321", "\n".join(logs.output))

    def test_yangyang_old_message_log_does_not_expose_code_in_subject(self):
        after = datetime.datetime(2026, 8, 1, 10, 2, 0).timestamp()

        with self.assertLogs("core.generic_api_mail_client", level="DEBUG") as logs:
            _fetch_yangyang_otp(
                FakeSession(),
                "http://yangyang.website/messages/tok/a@icloud.com",
                {"User-Agent": "test"},
                after_ts=after,
            )

        self.assertNotIn("654321", "\n".join(logs.output))

    def test_yangyang_non_200_log_does_not_expose_response_code(self):
        with self.assertLogs("core.generic_api_mail_client", level="DEBUG") as logs:
            result = _fetch_yangyang_otp(
                FakeYangyangErrorSession(),
                "http://yangyang.website/messages/tok/a@icloud.com",
                {"User-Agent": "test"},
            )

        output = "\n".join(logs.output)
        self.assertIsNone(result)
        self.assertNotIn("654321", output)
        self.assertIn("HTTP 500", output)
        self.assertIn("has_body=True", output)
        self.assertIn("body_len=", output)

    def test_inline_non_200_log_does_not_expose_response_code(self):
        with self.assertLogs("core.generic_api_mail_client", level="DEBUG") as logs:
            result = _fetch_yangyang_otp(
                FakeInlineErrorSession(),
                "https://mail.ai1998.xyz/messages/tok/a%40icloud.com",
                {"User-Agent": "test"},
            )

        output = "\n".join(logs.output)
        self.assertIsNone(result)
        self.assertNotIn("654321", output)
        self.assertIn("HTTP 500", output)
        self.assertIn("has_body=True", output)
        self.assertIn("body_len=", output)

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
        repeated = _fetch_yangyang_otp(
            FakeInlineSession(),
            "https://mail.ai1998.xyz/messages/tok/cookies-benzene.48%40icloud.com",
            {"User-Agent": "test"},
        )
        code, meta = result
        self.assertEqual(code, "541409")
        self.assertEqual(meta["mail_id"], repeated[1]["mail_id"])
        self.assertTrue(str(meta["mail_id"]).startswith("inline-sha256-"))

    def test_inline_helper_skips_baseline_id_and_returns_new_same_code_with_skewed_time(self):
        result = _fetch_yangyang_otp(
            FakeInlineBaselineSkewSession(),
            "https://mail.ai1998.xyz/messages/tok/a%40icloud.com",
            {"User-Agent": "test"},
            baseline_message_ids=frozenset({"old-id"}),
        )

        code, meta = result
        self.assertEqual(code, "119006")
        self.assertEqual(meta["mail_id"], "new-id")

    def test_inline_fallback_identity_survives_new_message_inserted_at_top(self):
        code_url = "https://mail.ai1998.xyz/messages/tok/a%40icloud.com"
        old_code, old_meta = _fetch_yangyang_otp(
            FakeInlineInsertedMessageSession(include_new=False),
            code_url,
            {"User-Agent": "test"},
        )

        new_code, new_meta = _fetch_yangyang_otp(
            FakeInlineInsertedMessageSession(include_new=True),
            code_url,
            {"User-Agent": "test"},
            baseline_message_ids=frozenset({str(old_meta["mail_id"])}),
        )

        self.assertEqual(old_code, "119006")
        self.assertEqual(new_code, "119006")
        self.assertNotEqual(new_meta["mail_id"], old_meta["mail_id"])
        self.assertEqual(new_meta["received_at"], "2026-08-17 10:01:37")

    def test_inline_otp_log_does_not_expose_code_in_subject(self):
        with self.assertLogs("core.generic_api_mail_client", level="INFO") as logs:
            code, _meta = _fetch_yangyang_otp(
                FakeInlineSession("Your OpenAI code is 541409"),
                "https://mail.ai1998.xyz/messages/tok/cookies-benzene.48%40icloud.com",
                {"User-Agent": "test"},
            )

        output = "\n".join(logs.output)
        self.assertEqual(code, "541409")
        self.assertNotIn("541409", output)
        self.assertIn("has_subject=True", output)
        self.assertIn("subject_len=", output)

    def test_inline_old_message_log_does_not_expose_code_in_subject(self):
        after = datetime.datetime(2026, 8, 2, 13, 20, 0).timestamp()

        with self.assertLogs("core.generic_api_mail_client", level="DEBUG") as logs:
            result = _fetch_yangyang_otp(
                FakeInlineSession("Your OpenAI code is 654321"),
                "https://mail.ai1998.xyz/messages/tok/cookies-benzene.48%40icloud.com",
                {"User-Agent": "test"},
                after_ts=after,
            )

        output = "\n".join(logs.output)
        self.assertIsNone(result)
        self.assertNotIn("654321", output)
        self.assertIn("has_subject=True", output)
        self.assertIn("subject_len=", output)

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
