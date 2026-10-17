from decimal import Decimal
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import main
from api.plaza import PlazaClient


def json_response(data, code=1, status=200):
    response = Mock(status_code=status)
    response.json.return_value = {"code": code, "data": data}
    return response


class PlazaClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.course = {"courseId": "101", "clazzId": "202"}
        self.client = PlazaClient(self.session, "https://tsjy.chaoxing.com/plaza/", self.course, "303")
        self.entry = {"mode": "study", "domain": "https://mooc1-1.chaoxing.com",
                      "classId": "202", "enc": "test-signed-context"}

    def test_score_requires_authenticated_ajax_headers_and_matching_course_context(self):
        self.session.get.return_value = json_response({"rule": {
            "totalScore": "31.5", "dayScore": 2, "courseMinScore": 100, "finishMustRead": False}})
        score = self.client.get_score()
        self.assertEqual(score["totalScore"], Decimal("31.5"))
        self.assertEqual(score["dayScore"], Decimal(2))
        call = self.session.get.call_args
        self.assertEqual(call.args[0], "https://tsjy.chaoxing.com/plaza/score/101/day-score")
        self.assertEqual(call.kwargs["params"], {"classId": "202", "userId": "303"})
        self.assertEqual(call.kwargs["headers"]["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual(parse_qs(urlsplit(call.kwargs["headers"]["Referer"]).query),
                         {"courseId": ["101"], "classId": ["202"], "userId": ["303"]})
        self.assertFalse(score["finishMustRead"])

    def test_permission_failure_never_becomes_zero_score(self):
        self.session.get.return_value = json_response(None, code=0)
        with self.assertRaisesRegex(RuntimeError, "未授权"):
            self.client.get_score()

    def test_missing_or_non_numeric_scores_fail_verification(self):
        for rule in ({}, {"totalScore": 0}, {"totalScore": "NaN", "dayScore": 0},
                     {"totalScore": True, "dayScore": 0}, {"totalScore": 1, "dayScore": "unknown"}):
            with self.subTest(rule=rule):
                self.session.get.return_value = json_response({"rule": rule})
                with self.assertRaises(RuntimeError):
                    self.client.get_score()

    def test_non_json_error_does_not_leak_response_body(self):
        self.session.get.return_value = Mock(status_code=200)
        self.session.get.return_value.json.side_effect = ValueError("secret response")
        with self.assertRaises(RuntimeError) as error:
            self.client.get_score()
        self.assertNotIn("secret response", str(error.exception))

    def test_explicit_plaza_url_must_be_trusted_https(self):
        for url in ("http://tsjy.chaoxing.com/plaza/", "https://chaoxing.com.example.org/plaza/",
                    "https://localhost/plaza/", "https://user:password@tsjy.chaoxing.com/plaza/"):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                PlazaClient(self.session, url, self.course, "303")

    def test_enter_chapter_records_entry_then_follows_official_transfer(self):
        self.session.get.side_effect = [json_response(self.entry),
                                        Mock(status_code=200, text='<script>var utEnc = "test-utenc";</script>')]
        context = self.client.enter_chapter("404")
        self.assertEqual(context["utenc"], "test-utenc")
        entry_call, transfer_call = self.session.get.call_args_list
        self.assertEqual(entry_call.args[0],
                         "https://tsjy.chaoxing.com/plaza/user/101/404/modify-node")
        self.assertEqual(entry_call.kwargs["params"], {"classId": "202"})
        transfer_query = parse_qs(urlsplit(transfer_call.args[0]).query)
        self.assertEqual(transfer_query["refer"], [context["study_url"]])
        self.assertEqual(parse_qs(urlsplit(context["study_url"]).query)["chapterId"], ["404"])
        self.assertFalse(transfer_call.kwargs["allow_redirects"])

    def test_cached_entry_does_not_record_duplicate_visits(self):
        self.session.get.side_effect = [json_response(self.entry),
                                        Mock(status_code=200, text="var utEnc='test-utenc';")]
        first = self.client.enter_chapter("404")
        first["utenc"] = "modified-by-caller"
        self.assertEqual(self.client.enter_chapter("404")["utenc"], "test-utenc")
        self.assertEqual(self.session.get.call_count, 2)

    def test_preview_mode_is_not_treated_as_authenticated_learning(self):
        self.session.get.return_value = json_response(dict(self.entry, mode="preview"))
        with self.assertRaisesRegex(RuntimeError, "未允许正式学习"):
            self.client.enter_chapter("404")
        self.assertEqual(self.session.get.call_count, 1)

    def test_untrusted_domain_is_not_followed(self):
        self.session.get.return_value = json_response(dict(self.entry, domain="https://evil.example"))
        with self.assertRaises(RuntimeError):
            self.client.enter_chapter("404")
        self.assertEqual(self.session.get.call_count, 1)

    def test_untrusted_redirect_is_not_followed(self):
        self.session.get.side_effect = [json_response(self.entry),
                                        Mock(status_code=302, headers={"Location": "https://evil.example"})]
        with self.assertRaises(RuntimeError):
            self.client.enter_chapter("404")
        self.assertEqual(self.session.get.call_count, 2)

    def test_trusted_redirect_and_html_token_are_supported(self):
        self.session.get.side_effect = [json_response(self.entry),
                                        Mock(status_code=302, headers={"Location": "/mycourse/studentstudy"}),
                                        Mock(status_code=200, text='<input id="utEnc" value="test-utenc">')]
        self.assertEqual(self.client.enter_chapter("404")["utenc"], "test-utenc")
        self.assertEqual(self.session.get.call_count, 3)

    def test_login_page_without_context_is_not_success(self):
        self.session.get.side_effect = [json_response(self.entry), Mock(status_code=200, text="Login required")]
        with self.assertRaisesRegex(RuntimeError, "登录学习上下文"):
            self.client.enter_chapter("404")


class PlazaCourseTests(unittest.TestCase):
    def setUp(self):
        self.logger = patch.object(main, "logger").start()
        self.factory = patch.object(main, "PlazaClient").start()
        self.client = self.factory.return_value
        patch.object(main.SessionManager, "get_session", return_value=Mock()).start()
        self.tasks = patch.object(main, "_process_course_tasks",
                                  return_value={"total": 1, "success": 1, "failed": 0, "pending": 0}).start()
        self.addCleanup(patch.stopall)
        self.api = Mock()
        self.api.get_uid.return_value = "303"
        self.course = {"title": "Test course", "courseId": "101", "clazzId": "202"}
        self.config = {"plaza_url": "https://tsjy.chaoxing.com/plaza/"}
        self.before = {"totalScore": Decimal(10), "dayScore": Decimal(1)}
        self.after = {"totalScore": Decimal(12), "dayScore": Decimal(3)}

    def test_normal_course_does_not_use_plaza(self):
        summary = main.process_course(self.api, self.course, {})
        self.assertEqual(summary["success"], 1)
        self.factory.assert_not_called()

    def test_plaza_context_available_during_tasks_and_real_score_delta_returned(self):
        self.client.get_score.side_effect = [self.before, self.after]

        def tasks(_api, course, _config):
            self.assertIs(course["_plaza"], self.client)
            return {"success": 1}

        self.tasks.side_effect = tasks
        summary = main.process_course(self.api, self.course, self.config)
        self.assertEqual(summary["plaza_score"]["delta"], Decimal(2))
        self.assertNotIn("_plaza", self.course)
        self.assertEqual(self.client.get_score.call_count, 2)

    def test_score_failure_before_tasks_prevents_unverified_run(self):
        self.client.get_score.side_effect = RuntimeError("score unavailable")
        with self.assertRaisesRegex(RuntimeError, "score unavailable"):
            main.process_course(self.api, self.course, self.config)
        self.tasks.assert_not_called()

    def test_score_is_checked_after_failed_tasks_without_hiding_original_error(self):
        self.client.get_score.side_effect = [self.before, self.after]
        self.tasks.side_effect = RuntimeError("chapter failed")
        with self.assertRaisesRegex(RuntimeError, "chapter failed"):
            main.process_course(self.api, self.course, self.config)
        self.assertEqual(self.client.get_score.call_count, 2)
        self.assertNotIn("_plaza", self.course)

    def test_post_run_score_failure_fails_course(self):
        self.client.get_score.side_effect = [self.before, RuntimeError("score unavailable")]
        with self.assertRaisesRegex(RuntimeError, "score unavailable"):
            main.process_course(self.api, self.course, self.config)
        self.assertNotIn("_plaza", self.course)

    def test_no_score_gain_is_reported_as_no_gain(self):
        self.client.get_score.side_effect = [self.before, self.before]
        summary = main.process_course(self.api, self.course, self.config)
        self.assertEqual(summary["plaza_score"]["delta"], 0)
        self.assertTrue(any("未见积分增加" in str(call) for call in self.logger.warning.call_args_list))

    def test_plaza_cli_option_flows_into_config_and_api_kwargs(self):
        with patch.object(main.sys, "argv", ["main.py", "--plaza-url", self.config["plaza_url"]]):
            common, _, _ = main.build_config_from_args(main.parse_args())
        self.assertEqual(common["plaza_url"], self.config["plaza_url"])
        with patch.object(main, "Tiku"), patch.object(main, "Chaoxing") as api_factory:
            main.init_chaoxing(dict(common, username="test-account", password="test-password"), {})
        self.assertEqual(api_factory.call_args.kwargs["plaza_url"], self.config["plaza_url"])


if __name__ == "__main__":
    unittest.main()
