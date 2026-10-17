import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main


class MainResultsTests(unittest.TestCase):
    def setUp(self):
        self.logger = patch.object(main, "logger").start()
        patch.object(main.time, "sleep", return_value=None).start()
        self.addCleanup(patch.stopall)
        self.course = {"title": "Test course", "courseId": "1", "clazzId": "2", "cpi": "3"}
        self.point = {"id": "4", "title": "Test chapter", "has_finished": False}
        self.config = {"speed": 1, "jobs": 1, "notopen_action": "retry"}
        self.api = Mock()

    def scan_info(self, **overrides):
        scan = {"scanned": 1, "pending": 0, "unsupported": 0, "parse_errors": 0,
                "passed": 0, "read_passed": 0}
        scan.update(overrides)
        return {"task_scan": scan}

    def process(self, jobs, info):
        self.api.get_job_list.return_value = (jobs, info)
        return main.process_chapter(self.api, self.course, self.point, 1)

    def run_processor(self, result, action="retry"):
        task = main.ChapterTask(0, dict(self.point))
        processor = main.JobProcessor(self.api, self.course, [task],
                                      dict(self.config, notopen_action=action))
        processor.max_tries = 3
        errors = []

        def run():
            try:
                processor.run()
            except BaseException as exc:
                errors.append(exc)

        with patch.object(main, "process_chapter", side_effect=result if isinstance(result, BaseException) else None,
                          return_value=None if isinstance(result, BaseException) else result) as process:
            runner = threading.Thread(target=run, daemon=True)
            runner.start()
            runner.join(timeout=3)
            self.assertFalse(runner.is_alive(), "queue.join hung after a failed chapter")
            self.assertFalse(errors)
            self.assertEqual(processor.task_queue.unfinished_tasks, 0)
            self.assertTrue(all(not thread.is_alive() for thread in processor.threads))
            return processor, task, process.call_count

    def test_worker_exception_finishes_after_bounded_retries(self):
        processor, task, calls = self.run_processor(RuntimeError("secret URL must not be logged"))
        self.assertEqual(calls, 3)
        self.assertEqual(task.result, main.ChapterResult.ERROR)
        self.assertEqual(processor.failed_tasks, [task])
        self.assertIn("RuntimeError", task.failure_reason)
        self.assertNotIn("secret URL", str(self.logger.mock_calls))

    def test_worker_system_exit_does_not_leave_queue_unfinished(self):
        processor, task, calls = self.run_processor(SystemExit(2))
        self.assertEqual(calls, 3)
        self.assertEqual(processor.failed_tasks, [task])

    def test_error_retry_limit_is_failure(self):
        processor, task, calls = self.run_processor(main.ChapterResult.ERROR)
        self.assertEqual(calls, 3)
        self.assertEqual(processor.failed_tasks, [task])

    def test_not_open_continue_is_recorded_as_incomplete(self):
        processor, task, calls = self.run_processor(main.ChapterResult.NOT_OPEN, "continue")
        self.assertEqual(calls, 1)
        self.assertEqual(processor.failed_tasks, [task])
        self.assertEqual(task.result, main.ChapterResult.NOT_OPEN)

    def test_not_open_retry_limit_is_failure(self):
        processor, task, calls = self.run_processor(main.ChapterResult.NOT_OPEN)
        self.assertEqual(calls, 3)
        self.assertEqual(processor.failed_tasks, [task])

    def test_invalid_result_is_failure_without_retry_loop(self):
        processor, task, calls = self.run_processor(None)
        self.assertEqual(calls, 1)
        self.assertEqual(task.result, main.ChapterResult.ERROR)
        self.assertEqual(processor.failed_tasks, [task])

    def test_retry_can_recover_and_other_chapters_still_run(self):
        tasks = [main.ChapterTask(i, dict(self.point, id=str(i))) for i in range(4)]
        processor = main.JobProcessor(self.api, self.course, tasks, dict(self.config, jobs=2))
        attempts = {}
        lock = threading.Lock()

        def process(_api, _course, point, _speed):
            with lock:
                attempts[point["id"]] = attempts.get(point["id"], 0) + 1
                attempt = attempts[point["id"]]
            if point["id"] == "0" and attempt == 1:
                raise ValueError("temporary failure")
            return main.ChapterResult.SUCCESS

        with patch.object(main, "process_chapter", side_effect=process):
            runner = threading.Thread(target=processor.run, daemon=True)
            runner.start()
            runner.join(timeout=3)
        self.assertFalse(runner.is_alive())
        self.assertEqual(attempts, {"0": 2, "1": 1, "2": 1, "3": 1})
        self.assertFalse(processor.failed_tasks)
        self.assertEqual(processor.task_queue.unfinished_tasks, 0)
        self.assertTrue(all(task.result == main.ChapterResult.SUCCESS for task in tasks))

    def test_unverified_empty_scan_is_not_success(self):
        self.assertEqual(self.process([], {}), main.ChapterResult.ERROR)

    def test_verified_empty_page_can_finish(self):
        self.assertEqual(self.process([], {"verified_empty": True}), main.ChapterResult.SUCCESS)

    def test_scanned_completed_cards_have_no_pending_work(self):
        self.assertEqual(self.process([], self.scan_info(passed=2, read_passed=1)),
                         main.ChapterResult.SUCCESS)
        self.assertTrue(any("没有待处理的支持任务" in str(call) for call in self.logger.info.call_args_list))

    def test_empty_jobs_with_unresolved_scan_counts_fail(self):
        for counter in ("pending", "unsupported", "parse_errors"):
            with self.subTest(counter=counter):
                self.assertEqual(self.process([], self.scan_info(**{counter: 1})), main.ChapterResult.ERROR)

    def test_supported_jobs_run_before_unsupported_scan_is_reported(self):
        jobs = [{"type": "read"}]
        with patch.object(main, "process_job", return_value=main.StudyResult.SUCCESS) as process:
            self.assertEqual(self.process(jobs, self.scan_info(pending=1, unsupported=1)),
                             main.ChapterResult.ERROR)
        process.assert_called_once()

    def test_each_job_keeps_its_own_card_defaults(self):
        card_info = {"knowledgeid": "card-specific"}
        jobs = [{"type": "read", "_job_info": card_info}, {"type": "document"}]
        info = self.scan_info(pending=2)
        with patch.object(main, "process_job", return_value=main.StudyResult.SUCCESS) as process:
            self.assertEqual(self.process(jobs, info), main.ChapterResult.SUCCESS)
        called_defaults = {call.args[2]["type"]: call.args[3] for call in process.call_args_list}
        self.assertEqual(called_defaults["read"], card_info)
        self.assertEqual(called_defaults["document"], info)

    def test_successful_jobs_do_not_hide_unmatched_pending_or_parse_errors(self):
        with patch.object(main, "process_job", return_value=main.StudyResult.SUCCESS):
            for info in (self.scan_info(pending=2), self.scan_info(pending=1, parse_errors=1)):
                with self.subTest(info=info):
                    self.assertEqual(self.process([{"type": "read"}], info), main.ChapterResult.ERROR)

    def test_api_failure_result_makes_chapter_fail(self):
        with patch.object(main, "process_job", return_value=main.StudyResult.ERROR):
            self.assertEqual(self.process([{"type": "read"}], self.scan_info(pending=1)),
                             main.ChapterResult.ERROR)

    def test_course_raises_for_failed_chapter_and_restores_progress_formatter(self):
        self.api.get_course_point.return_value = {"points": [self.point]}
        old_formatter = main.tqdm.format_sizeof
        with patch.object(main, "process_chapter", return_value=main.ChapterResult.NOT_OPEN):
            with self.assertRaisesRegex(RuntimeError, "未完成章节"):
                main.process_course(self.api, self.course, dict(self.config, notopen_action="continue"))
        self.assertIs(main.tqdm.format_sizeof, old_formatter)

    def test_course_raises_if_processor_leaves_pending_chapter(self):
        self.api.get_course_point.return_value = {"points": [self.point]}
        with patch.object(main, "JobProcessor") as processor:
            processor.return_value.failed_tasks = []
            with self.assertRaisesRegex(RuntimeError, "PENDING"):
                main.process_course(self.api, self.course, self.config)

    def test_course_success_summary_counts_chapters(self):
        self.api.get_course_point.return_value = {"points": [self.point, dict(self.point, id="5")]}
        with patch.object(main, "process_chapter", return_value=main.ChapterResult.SUCCESS):
            summary = main.process_course(self.api, self.course, self.config)
        self.assertEqual(summary, {"total": 2, "success": 2, "failed": 0, "pending": 0})

    def test_course_without_readable_chapters_is_not_success(self):
        self.api.get_course_point.return_value = {"points": []}
        with self.assertRaisesRegex(RuntimeError, "未读取到任何章节"):
            main.process_course(self.api, self.course, self.config)

    def test_main_propagates_incomplete_course_and_never_sends_success_notice(self):
        notification = Mock()
        notification.get_notification_from_config.return_value = notification
        self.api.login.return_value = {"status": True}
        with patch.object(main, "init_config", return_value=(dict(self.config), {}, {})), \
                patch.object(main, "configure_console_logger"), \
                patch.object(main, "init_chaoxing", return_value=self.api), \
                patch.object(main, "Notification", return_value=notification), \
                patch.object(main, "filter_courses", return_value=[self.course]), \
                patch.object(main, "process_course", side_effect=RuntimeError("incomplete course: enc=private-url")):
            with self.assertRaises(SystemExit) as caught:
                main.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertTrue(notification.send.called)
        self.assertFalse(any("未发现失败" in str(call) for call in notification.send.call_args_list))
        self.assertFalse(any("所有课程学习任务已完成" in str(call) for call in self.logger.info.call_args_list))
        self.assertNotIn("private-url", str(self.logger.mock_calls))
        self.assertNotIn("private-url", str(notification.mock_calls))

    def test_plaza_task_processing_never_overlaps_readings(self):
        self.course["_plaza"] = object()
        jobs = [{"type": "read", "jobid": str(index)} for index in range(5)]
        active = 0
        peak = 0
        lock = threading.Lock()

        def process(*_):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            threading.Event().wait(0.005)
            with lock:
                active -= 1
            return main.StudyResult.SUCCESS

        with patch.object(main, "process_job", side_effect=process):
            result = self.process(jobs, self.scan_info(pending=5))
        self.assertEqual(result, main.ChapterResult.SUCCESS)
        self.assertEqual(peak, 1)

    def test_plaza_chapter_workers_are_sequential_despite_jobs_option(self):
        tasks = [main.ChapterTask(index, dict(self.point, id=str(index))) for index in range(4)]
        processor = main.JobProcessor(self.api, self.course, tasks,
                                      dict(self.config, jobs=4, plaza_url="https://tsjy.chaoxing.com/plaza/"))
        active = 0
        peak = 0
        lock = threading.Lock()

        def process(*_):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            threading.Event().wait(0.005)
            with lock:
                active -= 1
            return main.ChapterResult.SUCCESS

        with patch.object(main, "process_chapter", side_effect=process):
            processor.run()
        self.assertEqual(peak, 1)
        self.assertEqual(len(processor.threads), 1)
        self.assertFalse(processor.failed_tasks)

    def test_ordinary_course_preserves_configured_chapter_workers(self):
        processor = main.JobProcessor(self.api, self.course, [], dict(self.config, jobs=4))
        self.assertEqual(processor.worker_num, 4)


class ReadingConfigTests(unittest.TestCase):
    def parse(self, *args):
        with patch.object(main.sys, "argv", ["main.py", *args]):
            return main.parse_args()

    def test_cli_default_and_explicit_real_reading_interval(self):
        args = self.parse()
        config, _, _ = main.build_config_from_args(args)
        self.assertEqual(config["read_seconds"], 120)
        args = self.parse("--read-seconds", "300", "--plaza-url", "https://tsjy.chaoxing.com/plaza/")
        config, _, _ = main.build_config_from_args(args)
        self.assertEqual(config["read_seconds"], 300)

    def test_cli_rejects_out_of_bounds_and_noninteger_intervals(self):
        for value in ("59", "601", "0", "NaN", "120.5"):
            with self.subTest(value=value), patch("sys.stderr"):
                with self.assertRaises(SystemExit) as caught:
                    self.parse("--read-seconds", value)
                self.assertEqual(caught.exception.code, 2)

    def test_config_duration_is_loaded_and_explicit_cli_can_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.ini"
            path.write_text("[common]\nread_seconds=300\n", encoding="utf8")
            with patch.object(main.sys, "argv", ["main.py", "-c", str(path)]):
                config, _, _ = main.init_config()
            self.assertEqual(config["read_seconds"], 300)
            # An explicit default value must override the file just as other values do.
            with patch.object(main.sys, "argv", ["main.py", "-c", str(path), "--read-seconds=120"]):
                config, _, _ = main.init_config()
            self.assertEqual(config["read_seconds"], 120)

    def test_invalid_file_interval_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.ini"
            path.write_text("[common]\nread_seconds=10\n", encoding="utf8")
            with self.assertRaisesRegex(ValueError, "60至600"):
                main.load_config_from_file(path)

    def test_interval_is_passed_to_chaoxing(self):
        with patch.object(main, "Tiku"), patch.object(main, "Chaoxing") as chaoxing:
            main.init_chaoxing({"use_cookies": True, "read_seconds": 180}, {})
        self.assertEqual(chaoxing.call_args.kwargs["read_seconds"], 180)

    def test_invalid_interval_fails_before_login(self):
        config = {"speed": 1, "jobs": 1, "read_seconds": 1}
        with patch.object(main, "init_config", return_value=(config, {}, {})), \
                patch.object(main, "configure_console_logger"), \
                patch.object(main, "logger"), \
                patch.object(main, "init_chaoxing") as initialize:
            with self.assertRaises(SystemExit) as caught:
                main.main()
        self.assertEqual(caught.exception.code, 1)
        initialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
