import json
import unittest
from unittest.mock import Mock, patch

from api import base, decode


def card_page(attachments, **defaults):
    data = {"defaults": {"knowledgeid": "chapter-1", **defaults},
            "attachments": attachments}
    return "<script>var mArg = " + json.dumps(data) + "; after();</script>"


def read_card(**changes):
    card = {"type": "read", "job": True, "jobid": "read-1", "jtoken": "test-token",
            "property": {"title": "A title with spaces", "read": True, "id": "resource-1"}}
    card.update(changes)
    return card


class CardDecodeTests(unittest.TestCase):
    def setUp(self):
        self.logger = patch.object(decode, "logger").start()
        self.addCleanup(patch.stopall)

    def test_read_with_job_true_is_not_silently_ignored(self):
        jobs, info = decode.decode_course_card(card_page([read_card()]))
        self.assertEqual([job["jobid"] for job in jobs], ["read-1"])
        self.assertEqual(jobs[0]["title"], "A title with spaces")
        self.assertEqual(info["task_scan"]["pending"], 1)
        self.assertEqual(info["task_scan"]["read_passed"], 0)

    def test_property_read_flag_is_not_completion_state(self):
        for property_data in ({"read": True}, {"read": False}, {}):
            with self.subTest(property_data=property_data):
                jobs, info = decode.decode_course_card(card_page([read_card(property=property_data)]))
                self.assertEqual(len(jobs), 1)
                self.assertEqual(info["task_scan"]["read_passed"], 0)

    def test_legacy_read_without_job_marker_is_still_recognized(self):
        card = read_card()
        del card["job"]
        jobs, _ = decode.decode_course_card(card_page([card]))
        self.assertEqual(len(jobs), 1)

    def test_read_explicit_false_marker_means_already_passed(self):
        for marker in (False, 0, "false", "0"):
            with self.subTest(marker=marker):
                jobs, info = decode.decode_course_card(card_page([read_card(job=marker)]))
                self.assertFalse(jobs)
                self.assertEqual(info["task_scan"]["read_passed"], 1)

    def test_is_passed_is_checked_before_task_processing(self):
        for marker in (True, 1, "true", "1"):
            with self.subTest(marker=marker):
                jobs, info = decode.decode_course_card(card_page([read_card(isPassed=marker)]))
                self.assertFalse(jobs)
                self.assertEqual(info["task_scan"]["passed"], 1)

    def test_incomplete_read_metadata_is_not_a_successful_empty_scan(self):
        for missing in ("jobid", "jtoken"):
            with self.subTest(missing=missing):
                card = read_card()
                del card[missing]
                jobs, info = decode.decode_course_card(card_page([card]))
                self.assertFalse(jobs)
                self.assertEqual(info["task_scan"]["unsupported"], 1)

    def test_required_video_parse_failure_is_counted(self):
        jobs, info = decode.decode_course_card(card_page([
            {"type": "video", "job": True, "jobid": "video-1", "property": {}}
        ]))
        self.assertFalse(jobs)
        self.assertEqual(info["task_scan"]["parse_errors"], 1)

    def test_unknown_required_task_is_reported_without_dumping_its_token(self):
        jobs, info = decode.decode_course_card(card_page([
            {"type": "new-task-type", "job": True, "private_token": "do-not-print-test-value"}
        ]))
        self.assertFalse(jobs)
        self.assertEqual(info["task_scan"]["unsupported"], 1)
        self.assertNotIn("do-not-print-test-value", str(self.logger.mock_calls))

    def test_card_defaults_are_attached_without_recursive_scan_metadata(self):
        jobs, info = decode.decode_course_card(card_page([read_card()], cardid="card-7", ktoken="test-context"))
        self.assertEqual(jobs[0]["_job_info"]["cardid"], "card-7")
        self.assertEqual(jobs[0]["_job_info"]["ktoken"], "test-context")
        self.assertNotIn("task_scan", jobs[0]["_job_info"])
        info["cardid"] = "later-card"
        self.assertEqual(jobs[0]["_job_info"]["cardid"], "card-7")

    def test_read_preview_context_survives_parsing(self):
        properties = {"id": "topic-1", "title": "A reading", "readtime": 5,
                      "_jobid": "embedded-job", "custom": {"nested": True}}
        jobs, _ = decode.decode_course_card(card_page([
            read_card(property=properties, _fromV2_="test-preview-context")
        ]))
        self.assertEqual(jobs[0]["property"], properties)
        self.assertEqual(jobs[0]["_fromV2_"], "test-preview-context")

    def test_malformed_attachment_container_is_not_verified_empty(self):
        for attachments in ({}, "", None):
            with self.subTest(attachments=attachments):
                with self.assertRaises(ValueError):
                    decode.decode_course_card(card_page(attachments))

    def test_missing_or_invalid_marg_is_not_reported_as_valid_empty_scan(self):
        self.assertEqual(decode.decode_course_card("<html>Login required</html>"), ([], {}))
        with self.assertRaises(json.JSONDecodeError):
            decode.decode_course_card("var mArg = {invalid};")


class JobListTests(unittest.TestCase):
    def setUp(self):
        patch.object(base, "logger").start()
        patch.object(decode, "logger").start()
        self.session = Mock()
        patch.object(base.SessionManager, "get_session", return_value=self.session).start()
        self.addCleanup(patch.stopall)
        self.api = object.__new__(base.Chaoxing)
        self.api.rate_limiter = Mock()
        self.course = {"courseId": "course-1", "clazzId": "class-1", "cpi": "cpi-1"}
        self.point = {"id": "chapter-1", "title": "Test chapter"}

    def respond(self, *pages):
        self.session.get.side_effect = [Mock(status_code=200, text=page) for page in pages]

    def test_duplicate_card_pages_do_not_run_same_task_multiple_times(self):
        self.respond(*(card_page([read_card()], cardid="card-1") for _ in range(7)))
        jobs, info = self.api.get_job_list(self.course, self.point)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(info["task_scan"]["pending"], 1)
        self.assertEqual(jobs[0]["_card_num"], "0")
        self.assertEqual(self.session.get.call_count, 7)

    def test_separate_cards_preserve_their_own_context(self):
        pages = [card_page([read_card(jobid="read-1")], cardid="card-1", ktoken="context-1"),
                 card_page([read_card(jobid="read-2")], cardid="card-2", ktoken="context-2")]
        self.respond(*pages, *("<html></html>" for _ in range(5)))
        jobs, info = self.api.get_job_list(self.course, self.point)
        self.assertEqual([job["_job_info"]["ktoken"] for job in jobs], ["context-1", "context-2"])
        self.assertEqual([job["_card_num"] for job in jobs], ["0", "1"])
        self.assertEqual(info["task_scan"]["pending"], 2)

    def test_no_valid_card_page_raises_instead_of_empty_success(self):
        self.respond(*("<html>Login page</html>" for _ in range(7)))
        with self.assertRaisesRegex(RuntimeError, "有效章节任务数据"):
            self.api.get_job_list(self.course, self.point)

    def test_http_error_is_not_ignored_after_an_earlier_valid_card(self):
        self.session.get.side_effect = [Mock(status_code=200, text=card_page([])), Mock(status_code=503)]
        with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
            self.api.get_job_list(self.course, self.point)

    def test_parse_failure_prevents_verified_empty(self):
        self.respond(card_page([{"type": "video", "job": True, "jobid": "video-1"}]),
                     *("<html></html>" for _ in range(6)))
        jobs, info = self.api.get_job_list(self.course, self.point)
        self.assertFalse(jobs)
        self.assertFalse(info["verified_empty"])
        self.assertEqual(info["task_scan"]["parse_errors"], 1)

    def test_all_explicitly_completed_tasks_have_verified_empty_result(self):
        self.respond(card_page([read_card(job=False)]), *("<html></html>" for _ in range(6)))
        jobs, info = self.api.get_job_list(self.course, self.point)
        self.assertFalse(jobs)
        self.assertTrue(info["verified_empty"])
        self.assertEqual(info["task_scan"]["read_passed"], 1)

    def test_not_open_page_stops_scan_without_marking_it_completed(self):
        self.respond("章节未开放")
        self.assertEqual(self.api.get_job_list(self.course, self.point), ([], {"notOpen": True}))
        self.assertEqual(self.session.get.call_count, 1)


class ReadResponseTests(unittest.TestCase):
    def setUp(self):
        self.logger = patch.object(base, "logger").start()
        self.session = Mock()
        patch.object(base.SessionManager, "get_session", return_value=self.session).start()
        self.addCleanup(patch.stopall)
        self.api = object.__new__(base.Chaoxing)
        self.course = {"courseId": "course-1", "clazzId": "class-1"}
        self.job = {"jobid": "read-1", "jtoken": "test-token", "title": "Test reading"}
        self.info = {"knowledgeid": "chapter-1"}

    def run_read(self, payload, http_status=200):
        response = Mock(status_code=http_status)
        response.json.return_value = payload
        self.session.get.return_value = response
        return self.api.study_read(self.course, self.job, self.info)

    def test_http_200_without_business_success_is_failure(self):
        for payload in ({}, {"status": False}, {"status": "false"}, {"status": 0}, [], None):
            with self.subTest(payload=payload):
                self.assertEqual(self.run_read(payload), base.StudyResult.ERROR)

    def test_non_json_response_is_failure(self):
        self.session.get.return_value = Mock(status_code=200)
        self.session.get.return_value.json.side_effect = ValueError("secret HTML must not be logged")
        self.assertEqual(self.api.study_read(self.course, self.job, self.info), base.StudyResult.ERROR)
        self.assertNotIn("secret HTML", str(self.logger.mock_calls))

    def test_http_failure_cannot_be_business_success(self):
        self.assertEqual(self.run_read({"status": True}, 500), base.StudyResult.ERROR)

    def test_explicit_business_success_and_official_endpoint(self):
        self.assertEqual(self.run_read({"status": True}), base.StudyResult.SUCCESS)
        self.assertEqual(self.session.get.call_args.kwargs["url"],
                         "https://mooc1.chaoxing.com/mooc-ans/job/readv2")
        self.assertEqual(self.session.get.call_args.kwargs["params"]["knowledgeid"], "chapter-1")


if __name__ == "__main__":
    unittest.main()
