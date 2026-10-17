"""Offline tests for reading metadata, trusted transport and elapsed-time gates."""

from datetime import datetime, timedelta
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

from api import reading


NOW = datetime(2026, 10, 17, 12, 0, tzinfo=reading.CHINA)
SOURCE = {"_from_": "signed%2Bsource", "rtag": "reading", "_fromV2_": "v2-context"}


def setting_for(source=None):
    source = SOURCE if source is None else source
    setting = {"resourceID": "900,901", "resourceType": "special", "from": 0,
               "passportUID": "123", "t": "20261017120000",
               "ext": json.dumps(source, ensure_ascii=False, separators=(",", ":"))}
    raw = "".join(str(setting[name]) for name in sorted(setting)) + "uWrVfLhv#1TW!@QA"
    setting["token"] = hashlib.md5(raw.encode()).hexdigest()
    return setting


def context_for(**changes):
    values = dict(origin="https://mooc1.chaoxing.com", preview_params={"enc": "private-preview"},
                  portal_url="https://mooc1.chaoxing.com/mooc-ans/zt/portal/private-topic",
                  source=SOURCE.copy(), setting=setting_for(), topic_course_id="900",
                  topic_chapter_id="901", content_text_count=100, image_count=1,
                  required_seconds=90, second_read_time=0, learn_mode=True,
                  is_course_square=True, job_id="job-1")
    values.update(changes)
    return reading.ReadingContext(**values)


def preview(seconds=0, learn="true", square="1", source=None):
    source = SOURCE if source is None else source
    return """<script>
        var from = '%s'; var rtag = '%s'; var _fromV2_ = '%s';
        var learnMode = '%s'; var secondReadTime = '%s'; var isCourseSquare = '%s';
        var id = 'encrypted/topic='; lookUrl = '/zt/portal/' + id;
        </script>""" % (source["_from_"], source["rtag"], source.get("_fromV2_", ""),
                       learn, seconds, square)


def response(text="", status=200, location=None):
    return SimpleNamespace(text=text, status_code=status,
                           headers={"Location": location} if location else {})


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds

    def now(self):
        return NOW + timedelta(seconds=self.value)


class TrustedTransportTests(unittest.TestCase):
    def test_relative_then_trusted_host_redirect_follows_location_query_only(self):
        session = Mock()
        session.get.side_effect = [response(status=302, location="/new?enc=redirect-private"),
                                   response(status=307, location="https://mooc1-1.chaoxing.com/final"),
                                   response("ok")]
        result = reading._get(session, "https://mooc1.chaoxing.com/old",
                              params={"enc": "original-private"}, referer="https://mooc1.chaoxing.com/")
        self.assertEqual(result.text, "ok")
        calls = session.get.call_args_list
        self.assertEqual(calls[1].args[0], "https://mooc1.chaoxing.com/new?enc=redirect-private")
        self.assertEqual(calls[0].kwargs["params"], {"enc": "original-private"})
        self.assertIsNone(calls[1].kwargs["params"])
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in calls))

    def test_untrusted_initial_urls_are_rejected_before_network(self):
        for url in ("http://mooc1.chaoxing.com/x", "https://chaoxing.com.evil.test/x",
                    "https://evil.test/x", "https://user:private@mooc1.chaoxing.com/x",
                    "https://mooc1.chaoxing.com:444/x", "https://mooc1.chaoxing.com:bad/x",
                    "https://data-xxt.aichaoxing.com/unrelated"):
            with self.subTest(url=url):
                session = Mock()
                with self.assertRaises(reading.ReadingError):
                    reading._get(session, url)
                session.get.assert_not_called()

    def test_untrusted_redirect_rejected_without_exposing_signed_url(self):
        session = Mock()
        session.get.return_value = response(status=302, location="https://evil.test/?enc=do-not-log")
        with self.assertRaises(reading.ReadingError) as caught:
            reading._get(session, "https://mooc1.chaoxing.com/preview?enc=private")
        self.assertEqual(session.get.call_count, 1)
        self.assertNotIn("do-not-log", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    def test_redirects_are_bounded(self):
        session = Mock()
        session.get.return_value = response(status=302, location="/loop")
        with self.assertRaisesRegex(reading.ReadingError, "重定向失败"):
            reading._get(session, "https://mooc1.chaoxing.com/start")
        self.assertEqual(session.get.call_count, 4)

    def test_collector_is_exact_and_cannot_redirect(self):
        session = Mock()
        session.get.return_value = response(status=302, location="https://mooc1.chaoxing.com/x")
        with self.assertRaisesRegex(reading.ReadingError, "重定向失败"):
            reading._get(session, reading._COLLECTOR + "?enc=private")
        self.assertEqual(session.get.call_count, 1)

    def test_request_exception_is_sanitized_and_not_chained(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError("https://host/?enc=private-secret")
        with self.assertRaises(reading.ReadingError) as caught:
            reading._get(session, "https://mooc1.chaoxing.com/preview")
        self.assertNotIn("private-secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)


class SettingTests(unittest.TestCase):
    def test_valid_signed_server_setting_is_accepted(self):
        reading._validate_setting(setting_for(), NOW)

    def test_mutated_context_or_token_is_rejected_without_sensitive_output(self):
        for key, value in (("resourceID", "999,901"), ("ext", "private-source"),
                           ("passportUID", "999"), ("token", "private-token")):
            with self.subTest(key=key):
                setting = setting_for()
                setting[key] = value
                with self.assertRaises(reading.ReadingError) as caught:
                    reading._validate_setting(setting, NOW)
                self.assertNotIn(value, str(caught.exception))

    def test_missing_signed_fields_and_bad_timestamp_are_rejected(self):
        for key in ("resourceID", "resourceType", "passportUID", "t", "token"):
            with self.subTest(key=key):
                setting = setting_for()
                del setting[key]
                with self.assertRaises(reading.ReadingError):
                    reading._validate_setting(setting, NOW)
        for timestamp in ("20261017", "20269917120000", "2026101712000X"):
            with self.subTest(timestamp=timestamp):
                setting = setting_for()
                setting["t"] = timestamp
                with self.assertRaisesRegex(reading.ReadingError, "时间戳无效"):
                    reading._validate_setting(setting, NOW)

    def test_expired_and_far_future_settings_are_rejected(self):
        for delta in (-18001, 18001):
            with self.subTest(delta=delta):
                with self.assertRaisesRegex(reading.ReadingError, "凭据已过期"):
                    reading._validate_setting(setting_for(), NOW + timedelta(seconds=delta))

    def test_context_repr_does_not_expose_signed_metadata(self):
        text = repr(context_for())
        for secret in ("private-preview", "private-topic", "v2-context", setting_for()["token"]):
            self.assertNotIn(secret, text)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.course = {"courseId": "teaching-course", "clazzId": "class-1"}
        self.job = {"jobid": "job-1", "enc": "server-enc", "_fromV2_": "card-context",
                    "property": {"jobid": "embedded-job", "_jobid": "djob", "readtime": 90}}
        self.info = {"knowledgeid": "teaching-chapter", "_plaza_context": {
            "study_url": "https://mooc1.chaoxing.com/study", "utenc": "server-utenc"}}
        self.portal = "<script>window['courseid']='900'; var ctid=901; var s={passportUID:'123'};</script>"
        self.cards = ("<script>window['t-901']='20261017120000'; window['token-901']='%s';</script>"
                      "<style>private style text</style><p>A😀</p><img src='x'>") % setting_for()["token"]
        self.datetime = patch.object(reading, "datetime", wraps=datetime).start()
        self.datetime.now.return_value = NOW
        self.addCleanup(patch.stopall)

    def prepare(self, preview_page=None, portal=None, cards=None):
        self.session.get.side_effect = [response(preview() if preview_page is None else preview_page),
                                        response(self.portal if portal is None else portal),
                                        response(self.cards if cards is None else cards)]
        return reading.prepare_reading(self.session, self.course, self.job, self.info)

    def test_signed_topic_metadata_is_used_instead_of_teaching_ids(self):
        context = self.prepare()
        self.assertEqual((context.topic_course_id, context.topic_chapter_id), ("900", "901"))
        self.assertEqual(context.setting["resourceID"], "900,901")
        self.assertEqual(context.source, SOURCE)
        self.assertEqual(context.content_text_count, 3)  # A plus a surrogate pair.
        self.assertEqual(context.image_count, 1)
        calls = self.session.get.call_args_list
        self.assertEqual(calls[0].kwargs["params"]["jobid"], "embedded-job")
        self.assertEqual(calls[0].kwargs["params"]["djobid"], "djob")
        self.assertEqual(calls[0].kwargs["params"]["_fromV2_"], "card-context")
        self.assertIn("encrypted%2Ftopic%3D", calls[1].args[0])
        self.assertEqual(calls[2].kwargs["params"]["knowledgeid"], "901")
        self.assertFalse(context.completion_allowed)

    def test_no_authorized_preview_or_user_is_not_successful_preparation(self):
        for page, portal in (("Login required", None), (None, self.portal.replace("'123'", "''"))):
            with self.subTest(page=page, portal=portal):
                with self.assertRaises(reading.ReadingError):
                    self.prepare(preview_page=page, portal=portal)

    def test_empty_article_and_missing_card_token_are_rejected(self):
        for cards in (self.cards[:self.cards.index("<style>")], "<p>A real text with no token</p>"):
            with self.subTest(cards=cards):
                with self.assertRaises(reading.ReadingError):
                    self.prepare(cards=cards)

    def test_nonlearning_preview_and_invalid_server_time_are_rejected(self):
        for page in (preview(learn=""), preview(seconds="NaN"), preview(seconds="-1"),
                     preview(seconds="not-a-number")):
            with self.subTest(page=page):
                with self.assertRaises(reading.ReadingError):
                    self.prepare(preview_page=page)


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.session = Mock()
        self.context = context_for()
        self.sent = []
        self.server_seconds = 0

        def get(url, **kwargs):
            if url.startswith(reading._COLLECTOR):
                query = parse_qs(urlsplit(url).query)
                payload = json.loads(query["d"][0])
                self.sent.append((self.clock.value, payload))
                return response()
            return response(preview(self.server_seconds))

        self.session.get.side_effect = get

    def observe(self, duration=90, **kwargs):
        return reading.observe_reading(self.session, self.context, duration,
                                       _monotonic=self.clock.monotonic, _sleep=self.clock.sleep,
                                       _now=self.clock.now, **kwargs)

    def test_ninety_real_seconds_send_start_three_heartbeats_and_end(self):
        progress = Mock()
        result = self.observe(on_progress=progress)
        self.assertEqual([(at, item["s"]) for at, item in self.sent],
                         [(0, 1), (30, 2), (60, 2), (90, 2), (90, 0)])
        self.assertEqual(sum(self.clock.sleeps), 90)
        self.assertEqual(result["elapsed_seconds"], 90)
        self.assertEqual(result["events"], 5)
        self.assertEqual([call.args[0] for call in progress.call_args_list], [30, 60, 90])
        self.assertTrue(all(item["h"] == 0 and item["e"] == "" for _, item in self.sent))
        self.assertEqual(self.sent[0][1]["r"], "900,901")

    def test_local_elapsed_time_does_not_override_server_completion_gate(self):
        result = self.observe()
        self.assertEqual(result["server_read_seconds"], 0)
        self.assertFalse(result["completion_allowed"])
        self.server_seconds = 90
        result = self.observe(duration=0, initial_read_log=False)
        self.assertTrue(result["completion_allowed"])
        self.assertEqual(result["server_read_seconds"], 90)

    def test_stop_request_ends_before_next_heartbeat_and_still_sends_end(self):
        result = self.observe(stop_requested=lambda: self.clock.value >= 10)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["elapsed_seconds"], 10)
        self.assertEqual([item["s"] for _, item in self.sent], [1, 0])

    def test_duration_limits_are_checked_before_any_request(self):
        for duration in (-1, 601, float("inf"), float("nan"), "private-invalid", None):
            with self.subTest(duration=duration):
                with self.assertRaises(ValueError):
                    self.observe(duration)
        self.session.get.assert_not_called()

    def test_heartbeat_failure_sends_end_and_preserves_first_safe_error(self):
        original = self.session.get.side_effect

        def fail(url, **kwargs):
            if url.startswith(reading._COLLECTOR):
                payload = json.loads(parse_qs(urlsplit(url).query)["d"][0])
                if payload["s"] == 2:
                    raise requests.Timeout("heartbeat-private-url")
                if payload["s"] == 0:
                    return response(status=503)
            return original(url, **kwargs)

        self.session.get.side_effect = fail
        with self.assertRaises(reading.ReadingError) as caught:
            self.observe()
        self.assertEqual(str(caught.exception), "阅读计时记录请求失败")
        self.assertNotIn("private", str(caught.exception))
        calls = self.session.get.call_args_list
        end = json.loads(parse_qs(urlsplit(calls[-1].args[0]).query)["d"][0])
        self.assertEqual(end["s"], 0)
        self.assertEqual(self.clock.value, 30)

    def test_keyboard_interrupt_is_preserved_after_cleanup(self):
        def interrupt(_):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            reading.observe_reading(self.session, self.context, 90,
                                    _monotonic=self.clock.monotonic, _sleep=interrupt,
                                    _now=self.clock.now)
        self.assertEqual([item["s"] for _, item in self.sent], [1, 0])

    def test_failed_final_event_does_not_report_completion(self):
        original = self.session.get.side_effect

        def fail(url, **kwargs):
            if url.startswith(reading._COLLECTOR):
                payload = json.loads(parse_qs(urlsplit(url).query)["d"][0])
                if payload["s"] == 0:
                    return response(status=503)
            return original(url, **kwargs)

        self.session.get.side_effect = fail
        with self.assertRaisesRegex(reading.ReadingError, "HTTP 503"):
            self.observe(0)
        self.assertEqual(self.session.get.call_count, 3)  # readlog, start, failed end.
        self.assertTrue(self.session.get.call_args.args[0].startswith(reading._COLLECTOR))
        self.assertFalse(any("/readv2job/preview" in call.args[0]
                             for call in self.session.get.call_args_list))

    def test_task_gate_requires_learning_mode_and_job_even_without_duration(self):
        self.assertTrue(context_for(required_seconds=None).completion_allowed)
        self.assertFalse(context_for(required_seconds=None, learn_mode=False).completion_allowed)
        self.assertFalse(context_for(required_seconds=None, job_id="").completion_allowed)
        self.assertFalse(context_for(required_seconds="invalid").completion_allowed)


if __name__ == "__main__":
    unittest.main()
