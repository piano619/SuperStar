"""Authenticated reading-page preparation and bounded, real-time observation.

The reading collector's HTTP response is not proof of awarded credit. Callers
must verify the course's task status and score independently. No interaction,
scroll position, server setting token, or elapsed time is manufactured here.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import math
import re
import time
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
import requests


CHINA = timezone(timedelta(hours=8))
_URI_SAFE = "~!*'()-_."
_COLLECTOR = "https://data-xxt.aichaoxing.com/analysis/ac_mark"


class ReadingError(RuntimeError):
    """A safe error message that contains no signed URLs or credentials."""


@dataclass(repr=False)
class ReadingContext:
    origin: str
    preview_params: dict = field(repr=False)
    portal_url: str = field(repr=False)
    source: dict = field(repr=False)
    setting: dict = field(repr=False)
    topic_course_id: str
    topic_chapter_id: str
    content_text_count: int
    image_count: int
    required_seconds: object
    second_read_time: float
    learn_mode: bool
    is_course_square: bool
    job_id: str

    @property
    def completion_allowed(self):
        if not self.learn_mode or not self.job_id:
            return False
        if not self.is_course_square or self.required_seconds in (None, "", 0, False):
            return True
        try:
            return self.second_read_time >= float(self.required_seconds)
        except (TypeError, ValueError):
            return False


def _origin(url):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        valid = (parts.scheme == "https" and host.endswith(".chaoxing.com")
                 and not parts.username and not parts.password
                 and parts.port in (None, 443))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ReadingError("阅读页面地址不受信任")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _get(session, url, *, params=None, referer=None, stage="页面"):
    # Redirects are expected when the platform renames a preview endpoint.
    # Inspect each destination before sending signed parameters or Referer.
    # The external analytics collector is allowed only at its exact endpoint;
    # it cannot redirect a signed event to an unrelated host.
    try:
        collector = urlsplit(url)._replace(query="", fragment="").geturl() == _COLLECTOR
    except (ValueError, TypeError):
        raise ReadingError("阅读页面地址不受信任") from None
    if not collector:
        _origin(url)
    headers = {"Referer": referer} if referer else {}
    for redirect in range(4):
        try:
            response = session.get(url, params=params, headers=headers, timeout=30,
                                   allow_redirects=False)
        except requests.RequestException:
            raise ReadingError(f"阅读{stage}请求失败") from None
        if response.status_code == 200:
            return response
        if response.status_code not in (301, 302, 303, 307, 308):
            raise ReadingError(f"阅读{stage}请求失败，HTTP {response.status_code}")
        location = response.headers.get("Location")
        if not location or redirect == 3 or collector:
            raise ReadingError(f"阅读{stage}重定向失败")
        url = urljoin(url, location)
        _origin(url)
        # The redirect Location already carries its own query string.
        params = None


def _js_string(page, name, *, required=False):
    match = re.search(r"\b" + re.escape(name) + r"\s*=\s*(['\"])(.*?)\1", page, re.S)
    if not match:
        if required:
            raise ReadingError("阅读页面缺少必要的登录上下文")
        return ""
    value = match.group(2).replace(r"\/", "/").replace(r'\"', '"').replace(r"\'", "'")
    return html.unescape(value)


def _scalar(value):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (str, int)):
        return str(value)
    raise ReadingError("阅读页面签名字段格式无效")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _validate_setting(setting, now):
    for name in ("resourceID", "resourceType", "passportUID", "t", "token"):
        if not setting.get(name):
            raise ReadingError("阅读页面没有返回完整的登录阅读凭据")
    try:
        stamp = _scalar(setting["t"])
        if len(stamp) != 14 or not stamp.isdigit():
            raise ValueError
        issued = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=CHINA)
    except (ValueError, TypeError):
        raise ReadingError("阅读页面时间戳无效") from None
    if abs((now - issued).total_seconds()) > 18000:
        raise ReadingError("阅读页面凭据已过期，需要重新打开页面")
    exclude = {name for name in str(setting.get("excludeFields", "")).split(",")
               if name != "excludeFields"}
    names = sorted(name for name in setting if name != "token" and name not in exclude)
    raw = "".join(_scalar(setting[name]) for name in names) + "uWrVfLhv#1TW!@QA"
    expected = hashlib.md5(raw.encode("utf8")).hexdigest()
    if expected.lower() != str(setting["token"]).lower():
        raise ReadingError("阅读页面凭据与当前专题上下文不匹配")


def _preview_state(context, page):
    context.learn_mode = bool(_js_string(page, "learnMode"))
    context.is_course_square = _js_string(page, "isCourseSquare") == "1"
    try:
        context.second_read_time = float(_js_string(page, "secondReadTime") or 0)
    except ValueError:
        raise ReadingError("平台返回的阅读时长无效") from None
    if not math.isfinite(context.second_read_time) or context.second_read_time < 0:
        raise ReadingError("平台返回的阅读时长无效")


def prepare_reading(session, course, job, info):
    """Fetch the real authorized preview, topic page and its first content card.

    This function only prepares content. It does not send the analytics event
    or mark a task complete. Signed fields are never synthesized or logged.
    """
    plaza = info.get("_plaza_context") or {}
    study_url = plaza.get("study_url", "https://mooc1.chaoxing.com/")
    origin = _origin(study_url)
    prop = job.get("property") or {}
    params = {
        "jobid": prop.get("jobid", job.get("jobid", "")),
        "djobid": prop.get("_jobid", ""), "type": "read",
        "knowledgeid": info["knowledgeid"], "ut": "s", "isphone": "false",
        "clazzId": course["clazzId"], "enc": job.get("enc", ""),
        "utenc": plaza.get("utenc", ""), "courseid": course["courseId"],
    }
    if job.get("_fromV2_"):
        params["_fromV2_"] = job["_fromV2_"]
    preview = _get(session, origin + "/mooc-ans/readv2job/preview", params=params,
                   referer=study_url, stage="预览").text
    source = {"_from_": _js_string(preview, "from"), "rtag": _js_string(preview, "rtag")}
    from_v2 = _js_string(preview, "_fromV2_")
    if from_v2:
        source["_fromV2_"] = from_v2
    # The signed preview selects the encrypted topic, not the teaching course ID.
    matches = re.findall(r"\bid\s*=\s*(['\"])([^'\"]+)\1", preview)
    if "/zt/portal/" not in preview or not matches:
        raise ReadingError("当前阅读页面不是受支持的专题页面")
    encrypted = html.unescape(matches[-1][1])
    portal_base = origin + "/mooc-ans/zt/portal/" + quote(encrypted, safe="")
    portal = _get(session, portal_base, params=source, referer=study_url, stage="专题").text
    match_course = re.search(r"window\[['\"]courseid['\"]\]\s*=\s*['\"](\d+)['\"]", portal)
    match_chapter = re.search(r"\bvar\s+ctid\s*=\s*(\d+)", portal)
    match_uid = re.search(r"\bpassportUID\s*:\s*(['\"])(.*?)\1", portal)
    if not match_course or not match_chapter or not match_uid or not match_uid.group(2):
        raise ReadingError("专题页面缺少已登录的课程、章节或用户信息")
    topic_course = match_course.group(1)
    chapter = match_chapter.group(1)
    card_params = {"knowledgeid": chapter, "encrypted": encrypted, **source}
    card_params.setdefault("_fromV2_", "")
    cards = _get(session, origin + "/mooc-ans/zt/portal/getcards", params=card_params,
                 referer=portal_base, stage="正文").text
    values = {}
    for name in ("t", "token"):
        pattern = r"window\[['\"]" + name + "-" + chapter + r"['\"]\]\s*=\s*(['\"])(.*?)\1"
        match = re.search(pattern, cards, re.S)
        if not match:
            raise ReadingError("专题正文没有提供阅读凭据")
        values[name] = html.unescape(match.group(2))
    setting = {"resourceID": topic_course + "," + chapter, "resourceType": "special",
               "from": 0, "passportUID": html.unescape(match_uid.group(2)),
               "t": values["t"], "ext": _json(source), "token": values["token"]}
    _validate_setting(setting, datetime.now(CHINA))
    soup = BeautifulSoup(cards, "lxml")
    images = len(soup.find_all("img"))
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    # Count retrieved text, not invented reading/scroll activity. UTF-16 length
    # matches JavaScript for non-BMP characters in the loaded article.
    text = soup.get_text(" ", strip=True)
    if not text and not images:
        raise ReadingError("专题正文为空，不能开始计时")
    count = len(text.encode("utf-16-le")) // 2
    context = ReadingContext(origin, params, portal_base, source, setting,
                             topic_course, chapter, count, images,
                             prop.get("readtime"), 0, False, False, job.get("jobid", ""))
    _preview_state(context, preview)
    if not context.learn_mode:
        raise ReadingError("专题预览不处于正式学习模式")
    return context


def _event_url(context, state, now):
    # No fictitious interactions or scroll changes: an actively loaded article
    # can report an empty event sequence at unchanged scroll position.
    payload = {"a": None, "r": context.setting["resourceID"], "t": "special",
               "l": 1, "f": 0, "wc": context.content_text_count,
               "ic": context.image_count, "v": 2, "s": state, "h": 0, "e": "",
               "ext": unquote(context.setting["ext"])}
    params = {"f": "readPoint", "u": context.setting["passportUID"],
              "d": quote(_json(payload), safe=_URI_SAFE),
              "t": now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"}
    raw = "".join(_scalar(params[name]) for name in sorted(params)) + "NrRzLDpWB2JkeodIVAn4"
    params["enc"] = hashlib.md5(raw.encode("utf8")).hexdigest()
    # d is already encodeURIComponent(JSON). Do not encode it a second time.
    return _COLLECTOR + "?&" + "&".join(name + "=" + _scalar(value) for name, value in params.items())


def refresh_reading(session, context):
    """Update the completion gate from the platform's actual recorded time."""
    page = _get(session, context.origin + "/mooc-ans/readv2job/preview",
                params=context.preview_params, referer=context.portal_url,
                stage="时长核验").text
    _preview_state(context, page)
    return context.completion_allowed


def observe_reading(session, context, duration_seconds=90, *, initial_read_log=True,
                    stop_requested=None, on_progress=None,
                    _monotonic=time.monotonic, _sleep=time.sleep, _now=None):
    """Observe a bounded interval and report only real elapsed-time heartbeats.

    The default 90-second probe sends no mouse/scroll events. It never converts
    local waiting into a completed task: the returned completion gate uses a
    freshly fetched server preview. Callers should inspect the independent
    score/task state, including the documented server statistics delay.
    """
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError):
        raise ValueError("Reading observation must be between 0 and 600 seconds") from None
    if not math.isfinite(duration) or not 0 <= duration <= 600:
        raise ValueError("Reading observation must be between 0 and 600 seconds")
    clock = _now or (lambda: datetime.now(CHINA))
    _validate_setting(context.setting, clock())
    if initial_read_log:
        _get(session, context.origin + "/multimedia/readlog",
             params={"courseid": context.topic_course_id,
                     "chapterid": context.topic_chapter_id, **context.source, "h": 0},
             referer=context.portal_url, stage="正文加载记录")
    event_count = 0

    def send(state):
        nonlocal event_count
        _get(session, _event_url(context, state, clock()), referer=context.portal_url,
             stage="计时记录")
        event_count += 1

    start = _monotonic()
    deadline = start + duration
    send(1)
    stopped = False
    next_event = start + 30
    try:
        while _monotonic() < deadline:
            if stop_requested and stop_requested():
                stopped = True
                break
            _sleep(min(1, max(0, deadline - _monotonic())))
            current = _monotonic()
            if current >= next_event:
                send(2)
                next_event = current + 30
                if on_progress:
                    on_progress(int(current - start))
    except BaseException:
        # Best-effort stop reporting must not replace the original error or
        # cancellation with a second network failure containing less context.
        try:
            send(0)
        except ReadingError:
            pass
        raise
    else:
        send(0)
    refresh_reading(session, context)
    return {"elapsed_seconds": _monotonic() - start, "events": event_count,
            "stopped": stopped, "server_read_seconds": context.second_read_time,
            "completion_allowed": context.completion_allowed}
