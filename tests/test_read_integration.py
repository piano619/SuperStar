import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api.base import Chaoxing, StudyResult
from api.reading import ReadingError


class ReadIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cx = Chaoxing(read_seconds=120)
        self.plaza = Mock()
        self.plaza.get_score.return_value = {'totalScore': 436, 'dayScore': 2}
        self.course = {'courseId':'course', 'clazzId':'class', '_plaza':self.plaza}
        self.job = {'type':'read', 'jobid':'job', 'jtoken':'test-token', 'title':'Example'}
        self.info = {'knowledgeid':'chapter'}
        self.context = SimpleNamespace(origin='https://mooc1-1.chaoxing.com',
            second_read_time=40, required_seconds=None, completion_allowed=True)
        self.session = Mock()
        self.session.get.return_value.status_code = 200
        self.session.get.return_value.json.return_value = {'status':True}

    def run_read(self):
        with patch('api.base.SessionManager.get_session', return_value=self.session):
            return self.cx.study_read(self.course, self.job, self.info)

    def test_resume_uses_recorded_server_time(self):
        with patch('api.base.prepare_reading',return_value=self.context), \
             patch('api.base.observe_reading',return_value={'elapsed_seconds':80,'server_read_seconds':100}) as observe:
            self.assertEqual(self.run_read(),StudyResult.SUCCESS)
        self.assertEqual(observe.call_args.args[2],80)
        self.plaza.get_score.assert_called_once()
        self.assertEqual(self.session.get.call_args.kwargs['url'],
                         'https://mooc1-1.chaoxing.com/mooc-ans/job/readv2')

    def test_already_recorded_time_is_not_replayed(self):
        self.context.second_read_time=120
        with patch('api.base.prepare_reading',return_value=self.context), \
             patch('api.base.observe_reading') as observe:
            self.assertEqual(self.run_read(),StudyResult.SUCCESS)
        observe.assert_not_called()

    def test_required_time_unconfirmed_does_not_mark_complete(self):
        self.context.required_seconds=120
        self.context.completion_allowed=False
        with patch('api.base.prepare_reading',return_value=self.context), \
             patch('api.base.observe_reading',return_value={'elapsed_seconds':80,'server_read_seconds':40}):
            self.assertEqual(self.run_read(),StudyResult.ERROR)
        self.session.get.assert_not_called()

    def test_reading_network_failure_does_not_mark_complete(self):
        with patch('api.base.prepare_reading',side_effect=ReadingError('safe failure')):
            self.assertEqual(self.run_read(),StudyResult.ERROR)
        self.session.get.assert_not_called()

    def test_failed_score_check_does_not_claim_success(self):
        self.context.second_read_time=120
        self.plaza.get_score.side_effect=RuntimeError('score unavailable')
        with patch('api.base.prepare_reading',return_value=self.context):
            self.assertEqual(self.run_read(),StudyResult.ERROR)
        self.session.get.assert_not_called()

    def test_timed_reading_without_context_is_not_marked_complete(self):
        self.course.pop('_plaza')
        self.job['property']={'readtime':120}
        self.assertEqual(self.run_read(),StudyResult.ERROR)
        self.session.get.assert_not_called()
