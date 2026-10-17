# -*- coding: utf-8 -*-
import argparse
import configparser
import enum
import sys
import threading
import time
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass, field
from queue import PriorityQueue
from threading import RLock
from typing import Any

from tqdm import tqdm

from api.answer import Tiku
from api.base import Chaoxing, Account, StudyResult, SessionManager
from api.exceptions import LoginError, InputFormatError
from api.logger import configure_console_logger, logger
from api.notification import Notification
from api.live import Live
from api.live_process import LiveProcessor
from api.plaza import PlazaClient

class ChapterResult(enum.Enum):
    SUCCESS=0,
    ERROR=1,
    NOT_OPEN=2,
    PENDING=3


def log_error(func):
    def wrapper(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except BaseException as e:
            logger.error("Error in thread {}: {}", threading.current_thread().name, type(e).__name__)
            raise

    return wrapper


def str_to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def read_seconds_value(value):
    """Validate the real-time interval before creating a session or worker."""
    try:
        seconds = int(value)
        if isinstance(value, bool) or isinstance(value, float) and value != seconds:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("阅读计时必须是60至600之间的整数秒") from None
    if not 60 <= seconds <= 600:
        raise ValueError("阅读计时必须是60至600之间的整数秒")
    return seconds


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Samueli924/chaoxing",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("--use-cookies", action="store_true", help="使用cookies登录")

    parser.add_argument(
        "-c", "--config", type=str, default=None, help="使用配置文件运行程序"
    )
    parser.add_argument("-u", "--username", type=str, default=None, help="手机号账号")
    parser.add_argument("-p", "--password", type=str, default=None, help="登录密码")
    parser.add_argument(
        "-l", "--list", type=str, default=None, help="要学习的课程ID列表, 以 , 分隔"
    )
    parser.add_argument(
        "-s", "--speed", type=float, default=1.0, help="视频播放倍速 (默认1, 最大2)"
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=4, help="普通课程同时处理的章节数；学习广场固定顺序处理"
    )

    parser.add_argument(
        "-v",
        "--verbose",
        "--debug",
        action="store_true",
        help="启用调试模式, 输出DEBUG级别日志",
    )
    parser.add_argument(
        "-a", "--notopen-action", type=str, default="retry", 
        choices=["retry", "ask", "continue"],
        help="遇到关闭任务点时的行为: retry-重试, ask-询问, continue-继续"
    )

    parser.add_argument("--auto-sign", action="store_true", help="自动签到")
    parser.add_argument("--plaza-url", default=None,
                        help="显式启用学习广场入口和积分核验，例如 https://tsjy.chaoxing.com/plaza/")
    parser.add_argument("--read-seconds", type=read_seconds_value, default=120,
                        help="学习广场每篇阅读的实际计时秒数，范围60至600")

    # 在解析之前捕获 -h 的行为
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
        parser.print_help()
        sys.exit(0)

    return parser.parse_args()


def load_config_from_file(config_path):
    """从配置文件加载设置"""
    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf8")
    
    common_config: dict[str, Any] = {}
    tiku_config: dict[str, Any] = {}
    notification_config: dict[str, Any] = {}
    
    # 检查并读取common节
    if config.has_section("common"):
        common_config = dict(config.items("common"))
        # 处理course_list，将字符串转换为列表
        if "course_list" in common_config and common_config["course_list"]:
            common_config["course_list"] = [item.strip() for item in common_config["course_list"].split(",") if item.strip()]
        # 处理speed，将字符串转换为浮点数
        if "speed" in common_config:
            common_config["speed"] = float(common_config["speed"])
        if "jobs" in common_config:
            common_config["jobs"] = int(common_config["jobs"])
        if "read_seconds" in common_config:
            common_config["read_seconds"] = read_seconds_value(common_config["read_seconds"])
        # 处理notopen_action，设置默认值为retry
        if "notopen_action" not in common_config:
            common_config["notopen_action"] = "retry"
        if "use_cookies" in common_config:
            common_config["use_cookies"] = str_to_bool(common_config["use_cookies"])
        if "username" in common_config and common_config["username"] is not None:
            common_config["username"] = common_config["username"].strip()
        if "password" in common_config and common_config["password"] is not None:
            common_config["password"] = common_config["password"].strip()
        if "verbose" in common_config:
            common_config["verbose"] = str_to_bool(common_config["verbose"])

    # 检查并读取tiku节
    if config.has_section("tiku"):
        tiku_config = dict(config.items("tiku"))
        # 处理数值类型转换
        for key in ["delay", "cover_rate"]:
            if key in tiku_config:
                tiku_config[key] = float(tiku_config[key])

    # 检查并读取notification节
    if config.has_section("notification"):
        notification_config = dict(config.items("notification"))
    
    return common_config, tiku_config, notification_config


def build_config_from_args(args):
    """从命令行参数构建配置"""
    common_config = {
        "use_cookies": args.use_cookies,
        "username": args.username,
        "password": args.password,
        "course_list": [item.strip() for item in args.list.split(",") if item.strip()] if args.list else None,
        "speed": args.speed if args.speed else 1.0,
        "jobs": args.jobs,
        "notopen_action": args.notopen_action if args.notopen_action else "retry",
        "verbose": args.verbose,
        "plaza_url": args.plaza_url,
        "read_seconds": args.read_seconds,
    }
    return common_config, {}, {}


def init_config():
    """初始化配置"""
    args = parse_args()

    if args.config:
        common_config, tiku_config, notification_config = load_config_from_file(args.config)
        if args.verbose:
            common_config["verbose"] = True
        if args.plaza_url:
            common_config["plaza_url"] = args.plaza_url
        if any(arg == "--read-seconds" or arg.startswith("--read-seconds=") for arg in sys.argv[1:]):
            common_config["read_seconds"] = args.read_seconds
        common_config.setdefault("read_seconds", 120)
        return common_config, tiku_config, notification_config
    else:
        return build_config_from_args(args)



def init_chaoxing(common_config, tiku_config):
    """初始化超星实例"""
    username = common_config.get("username", "")
    password = common_config.get("password", "")
    use_cookies = common_config.get("use_cookies", False)
    
    # 如果没有提供用户名密码，从命令行获取
    if (not username or not password) and not use_cookies:
        username = input("请输入你的手机号, 按回车确认\n手机号:")
        password = input("请输入你的密码, 按回车确认\n密码:")
    
    account = Account(username, password)
    
    # 设置题库
    tiku = Tiku()
    tiku.config_set(tiku_config)  # 载入配置
    tiku = tiku.get_tiku_from_config()  # 载入题库
    tiku.init_tiku()  # 初始化题库
    
    # 获取查询延迟设置
    
    # 检查大模型连接（如果使用的是大模型题库）
    # 根据配置文件中的 provider 判断是否为大模型题库
    provider = tiku_config.get('provider', '')
    provider_list = [name.strip() for name in provider.split(',') if name.strip()]
    if any(name in ['AI', 'SiliconFlow'] for name in provider_list):
        check_connection = tiku_config.get('check_llm_connection', 'true').lower() == 'true'
        if check_connection:
            logger.info(f'正在验证大模型配置 (provider={provider})...')
            if not tiku.check_llm_connection():
                logger.error('大模型连接检查失败')
                choice = input('大模型连接检查失败，无法准确答题，是否继续运行？(Y/n): ').strip().lower()
                # 直接回车默认继续运行
                if choice not in ('', 'y', 'yes'):
                    raise RuntimeError('用户取消运行')
                logger.info('用户选择继续运行...')

    query_delay = tiku_config.get("delay", 0)
    
    # 实例化超星API
    chaoxing = Chaoxing(account=account, tiku=tiku, query_delay=query_delay,
                        plaza_url=common_config.get("plaza_url"),
                        read_seconds=read_seconds_value(common_config.get("read_seconds", 120)))
    
    return chaoxing

def process_job(chaoxing: Chaoxing, course: dict, job: dict, job_info: dict, speed: float) -> StudyResult:
    """处理单个任务点"""
    # 视频任务
    if job["type"] == "video":
        logger.trace(f"识别到视频任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        # 超星的接口没有返回当前任务是否为Audio音频任务
        video_result = chaoxing.study_video(
            course, job, job_info, _speed=speed, _type="Video"
        )
        if video_result.is_failure():
            logger.warning("当前任务非视频任务, 正在尝试音频任务解码")
            video_result = chaoxing.study_video(
                course, job, job_info, _speed=speed, _type="Audio")
        if video_result.is_failure():
            logger.warning(
                f"出现异常任务 -> 任务章节: {course['title']} 任务ID: {job['jobid']}, 已跳过"
            )
        return video_result
    # 文档任务
    elif job["type"] == "document":
        logger.trace(f"识别到文档任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        return chaoxing.study_document(course, job)
    # 测验任务
    elif job["type"] == "workid":
        logger.trace(f"识别到章节检测任务, 任务章节: {course['title']}")
        return chaoxing.study_work(course, job, job_info)
    # 阅读任务
    elif job["type"] == "read":
        logger.trace(f"识别到阅读任务, 任务章节: {course['title']}")
        return chaoxing.study_read(course, job, job_info)
    # 直播任务
    elif job["type"] == "live":
        logger.trace(f"识别到直播任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        try:
            # 准备直播所需参数
            defaults = {
                "userid": chaoxing.get_uid(),
                "clazzId": course.get("clazzId"),
                "knowledgeid": job_info.get("knowledgeid")
            }
            
            # 创建直播对象
            live = Live(
                attachment=job,
                defaults=defaults,
                course_id=course.get("courseId")
            )
            
            # 启动直播处理线程
            thread = threading.Thread(
                target=LiveProcessor.run_live,
                args=(live, speed),
                daemon=True
            )
            thread.start()
            thread.join()  # 等待直播处理完成
            return StudyResult.SUCCESS
        except Exception as e:
            logger.error("处理直播任务时出错: {}", type(e).__name__)
            return StudyResult.ERROR

    logger.error(f"未知任务类型: {job['type']}")
    return StudyResult.ERROR


@dataclass(order=True)
class ChapterTask:
    index: int
    point: dict[str, Any] = field(compare=False)
    result: ChapterResult = field(default=ChapterResult.PENDING, compare=False)
    tries: int = field(default=0, compare=False)
    failure_reason: str = field(default="", compare=False)

class JobProcessor:
    STOP_POINT = {"__stop__": True}

    def __init__(self, chaoxing: Chaoxing, course: dict[str, Any], tasks: list[ChapterTask], config: dict[str, Any]):
        if "jobs" not in config or not config["jobs"]:
            config["jobs"] = 4
        
        self.chaoxing = chaoxing
        self.course = course
        self.speed = config["speed"]
        self.max_tries = 5
        self.tasks = tasks
        self.failed_tasks: list[ChapterTask] = []
        self.task_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.threads: list[threading.Thread] = []
        self.worker_num = int(config["jobs"])
        if self.worker_num < 1:
            raise ValueError("章节并发数必须大于0")
        if config.get("plaza_url") or course.get("_plaza") is not None:
            if self.worker_num != 1:
                logger.info("学习广场按实际时长顺序处理，章节并发数调整为1")
            self.worker_num = 1
        self.config = config

    def _stop_task(self) -> ChapterTask:
        return ChapterTask(index=sys.maxsize, point=self.STOP_POINT)

    @staticmethod
    def _is_stop_task(task: ChapterTask) -> bool:
        return task.point.get("__stop__") is True

    def run(self):
        for task in self.tasks:
            self.task_queue.put(task)

        for i in range(self.worker_num):
            thread = threading.Thread(target=self.worker_thread, daemon=True)
            self.threads.append(thread)
            thread.start()

        self.task_queue.join()

        for _ in self.threads:
            self.task_queue.put(self._stop_task())

        for thread in self.threads:
            thread.join()

    def worker_thread(self):
        tqdm.set_lock(tqdm.get_lock())
        while True:
            task = self.task_queue.get()
            try:
                if self._is_stop_task(task):
                    return

                task.tries += 1
                task.failure_reason = ""
                try:
                    task.result = process_chapter(self.chaoxing, self.course, task.point, self.speed)
                except BaseException as exc:
                    # A worker exception must not leave an unfinished queue item behind.
                    # Avoid logging exception text, which can contain authenticated URLs.
                    task.result = ChapterResult.ERROR
                    task.failure_reason = f"处理异常: {type(exc).__name__}"
                    logger.error("章节处理异常: {} ({})", task.point["title"], type(exc).__name__)

                if task.result == ChapterResult.SUCCESS:
                    logger.debug("章节处理成功: {}", task.point["title"])
                    continue

                retry = task.result in (ChapterResult.ERROR, ChapterResult.NOT_OPEN)
                if task.result == ChapterResult.NOT_OPEN:
                    task.failure_reason = "章节未开放"
                    if self.config["notopen_action"] == "continue":
                        retry = False
                        task.failure_reason = "章节未开放，按配置跳过，仍未完成"
                elif task.result == ChapterResult.ERROR:
                    task.failure_reason = task.failure_reason or "任务处理失败或仍有未处理任务"
                else:
                    retry = False
                    task.result = ChapterResult.ERROR
                    task.failure_reason = "章节返回无效结果"

                if retry and task.tries < self.max_tries:
                    logger.warning("章节待重试: {} ({}/{}): {}", task.point["title"],
                                   task.tries, self.max_tries, task.failure_reason)
                    time.sleep(1)
                    # Enqueue before task_done so join cannot finish between attempts.
                    self.task_queue.put(task)
                else:
                    self.failed_tasks.append(task)
                    logger.error("章节未完成: {}，尝试{}次: {}", task.point["title"],
                                 task.tries, task.failure_reason)
            finally:
                self.task_queue.task_done()


def process_chapter(chaoxing: Chaoxing, course:dict[str, Any], point:dict[str, Any], speed:float) -> ChapterResult:
    """处理单个章节"""
    logger.info(f'当前章节: {point["title"]}')
    if point["has_finished"]:
        logger.info(f'章节：{point["title"]} 已完成所有任务点')
        return ChapterResult.SUCCESS
    
    # 随机等待，避免请求过快
    chaoxing.rate_limiter.limit_rate(random_time=True,random_min=0, random_max=0.2)
    
    # 获取当前章节的所有任务点
    job_info = None
    jobs, job_info = chaoxing.get_job_list(course, point)

    # 发现未开放章节, 根据配置处理
    if job_info.get("notOpen", False):
        return ChapterResult.NOT_OPEN

    scan = job_info.get("task_scan", {})
    pending = scan.get("pending", 0)
    unsupported = scan.get("unsupported", 0)
    parse_errors = scan.get("parse_errors", 0)
    scanned = scan.get("scanned", 0)
    logger.info("章节任务扫描: 扫描{}，已通过{}，已读{}，待处理{}，不支持{}，解析失败{}",
                scanned, scan.get("passed", 0), scan.get("read_passed", 0),
                pending, unsupported, parse_errors)

    if not jobs:
        if pending or unsupported or parse_errors:
            logger.error("章节仍有未处理任务: {} (待处理{}，不支持{}，解析失败{})",
                         point["title"], pending, unsupported, parse_errors)
            return ChapterResult.ERROR
        if not scanned and not job_info.get("verified_empty", False):
            logger.error("章节未取得可验证的任务扫描结果: {}", point["title"])
            return ChapterResult.ERROR
        logger.info("章节没有待处理的支持任务: {}", point["title"])
        return ChapterResult.SUCCESS

    # Plaza reading must use one real timeline rather than overlapping articles.
    max_workers = 1 if course.get("_plaza") is not None else 5
    job_results:list[StudyResult]=[]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for result in executor.map(
            lambda job: process_job(chaoxing, course, job, job.get("_job_info", job_info), speed), jobs
        ):
            job_results.append(result)
    
    for result in job_results:
        if result.is_failure():
            return ChapterResult.ERROR

    if unsupported or parse_errors or pending > len(jobs):
        logger.error("支持的任务已处理，但章节仍未完成: {} (待处理未匹配{}，不支持{}，解析失败{})",
                     point["title"], max(0, pending - len(jobs)), unsupported, parse_errors)
        return ChapterResult.ERROR

    return ChapterResult.SUCCESS



def _process_course_tasks(chaoxing: Chaoxing, course:dict[str, Any], config: dict):
    """处理单个课程"""
    logger.info(f"开始学习课程: {course['title']}")
    
    # 获取当前课程的所有章节
    point_list = chaoxing.get_course_point(
        course["courseId"], course["clazzId"], course["cpi"]
    )

    if not point_list.get("points"):
        raise RuntimeError("课程未读取到任何章节，无法确认任务状态")

    _old_format_sizeof = tqdm.format_sizeof
    tqdm.format_sizeof = format_time
    tqdm.set_lock(RLock())
    try:
        tasks = [ChapterTask(point=point, index=i) for i, point in enumerate(point_list["points"])]
        processor = JobProcessor(chaoxing, course, tasks, config)
        processor.run()
        pending = [task for task in tasks if task.result == ChapterResult.PENDING]
        failed = [task for task in tasks if task.result not in (ChapterResult.SUCCESS, ChapterResult.PENDING)]
        succeeded = sum(task.result == ChapterResult.SUCCESS for task in tasks)
        logger.info("课程章节处理结果: 共{}，成功{}，失败{}，待处理{}",
                    len(tasks), succeeded, len(failed), len(pending))
        if processor.failed_tasks or failed or pending:
            incomplete = failed + pending
            if not incomplete:
                incomplete = processor.failed_tasks
            details = "；".join(
                f"{task.point['title']}: {task.failure_reason or task.result.name}"
                for task in incomplete
            )
            raise RuntimeError(f"课程存在未完成章节 ({len(incomplete)}/{len(tasks)}): {details}")
        return {"total": len(tasks), "success": succeeded, "failed": 0, "pending": 0}
    finally:
        tqdm.format_sizeof = _old_format_sizeof


def _log_plaza_score(before, after):
    delta = after["totalScore"] - before["totalScore"]
    logger.info("学习广场积分核验: 总积分 {} → {}，变化 {}；今日积分 {} → {}",
                before["totalScore"], after["totalScore"], delta,
                before["dayScore"], after["dayScore"])
    if delta == 0:
        logger.warning("本次核验未见积分增加；任务接口成功不能代替积分或课程达标结果")
    logger.info("平台返回的课程要求: 最低积分 {}；必读完成状态 {}；每日积分提醒 {}",
                after.get("courseMinScore", "未知"), after.get("finishMustRead", "未知"),
                after.get("dayScoreLimitRemind", "未知"))


def process_course(chaoxing: Chaoxing, course:dict[str, Any], config: dict):
    """In plaza mode, verify platform scores independently of task API results."""
    plaza_url = config.get("plaza_url")
    if not plaza_url:
        return _process_course_tasks(chaoxing, course, config)
    plaza = PlazaClient(SessionManager.get_session(), plaza_url, course, chaoxing.get_uid())
    before = plaza.get_score()
    logger.info("学习广场运行前积分: 总积分 {}，今日积分 {}",
                before["totalScore"], before["dayScore"])
    course["_plaza"] = plaza
    try:
        try:
            summary = _process_course_tasks(chaoxing, course, config)
        except BaseException:
            try:
                _log_plaza_score(before, plaza.get_score())
            except Exception:
                logger.error("课程处理失败后，积分核验也失败；不能确认本次积分变化")
            raise
        after = plaza.get_score()
        _log_plaza_score(before, after)
        summary["plaza_score"] = {"before": before, "after": after,
                                  "delta": after["totalScore"] - before["totalScore"]}
        return summary
    finally:
        course.pop("_plaza", None)

def filter_courses(all_course, course_list):
    """过滤要学习的课程"""
    if not course_list:
        # 手动输入要学习的课程ID列表
        print("*" * 10 + "课程列表" + "*" * 10)
        for course in all_course:
            print(f"ID: {course['courseId']} 课程名: {course['title']}")
        print("*" * 28)
        try:
            course_list = input(
                "请输入想要学习的课程列表,以逗号分隔,例: 2151141,189191,198198\n"
            ).split(",")
        except Exception as e:
            raise InputFormatError("输入格式错误") from e
    course_list = [str(course_id).strip() for course_id in course_list if str(course_id).strip()]
    if not course_list:
        logger.warning("未指定课程ID，将学习全部课程")
        return all_course

    # 筛选需要学习的课程
    course_task = []
    course_ids = []
    for course in all_course:
        if course["courseId"] in course_list and course["courseId"] not in course_ids:
            course_task.append(course)
            course_ids.append(course["courseId"])
    
    if not course_task:
        raise InputFormatError(f"未找到指定课程ID: {', '.join(course_list)}")
    
    return course_task


def format_time(num, suffix='', divisor=''):
    total_time = round(num)
    sec = total_time % 60
    mins = (total_time % 3600) // 60
    hrs = total_time // 3600

    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{sec:02d}"

    return f"{mins:02d}:{sec:02d}"


def main():
    """主程序入口"""
    try:
        # 初始化配置
        common_config, tiku_config, notification_config = init_config()
        configure_console_logger("DEBUG" if common_config.get("verbose", False) else "INFO")
        
        # 强制播放按照配置文件调节
        common_config["speed"] = min(2.0, max(1.0, common_config.get("speed", 1.0)))
        common_config["notopen_action"] = common_config.get("notopen_action", "retry")
        common_config["read_seconds"] = read_seconds_value(common_config.get("read_seconds", 120))
        
        # 初始化超星实例
        chaoxing = init_chaoxing(common_config, tiku_config)
        
        # 设置外部通知
        notification = Notification()
        notification.config_set(notification_config)
        notification = notification.get_notification_from_config()
        notification.init_notification()
        
        # 检查当前登录状态
        _login_state = chaoxing.login(login_with_cookies=common_config.get("use_cookies", False))
        if not _login_state["status"]:
            raise LoginError(_login_state["msg"])
        
        # 获取所有的课程列表
        all_course = chaoxing.get_course_list()
        
        # 过滤要学习的课程
        course_task = filter_courses(all_course, common_config.get("course_list"))
        
        # 开始学习
        logger.info(f"课程列表过滤完毕, 当前课程任务数量: {len(course_task)}")
        for course in course_task:
            process_course(chaoxing, course, common_config)
        
        logger.info("本次已识别的课程任务处理结束，未发现失败或待处理章节")
        notification.send("chaoxing : 本次已识别的课程任务处理结束，未发现失败或待处理章节")
        
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if code != 0:
            logger.error("错误: 程序异常退出, 返回码: {}", code)
        raise SystemExit(code) from None
    except KeyboardInterrupt:
        logger.error("错误: 程序被用户手动中断")
        raise SystemExit(130) from None
    except BaseException as e:
        logger.error("错误: {}，处理未完成，请查看上述章节日志", type(e).__name__)
        try:
            notification.send(f"chaoxing : 处理未完成，错误类型 {type(e).__name__}，请查看章节日志")
        except Exception:
            pass  # 如果通知发送失败，忽略异常
        # Uncaught request exceptions may contain signed URLs. Keep a failing
        # process exit code without allowing Python to print those secrets.
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

