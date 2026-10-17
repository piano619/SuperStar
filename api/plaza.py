"""Authenticated entry and score checks for an explicitly configured course plaza."""

import html
import re
import threading
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


def _trusted_url(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or not (host == "chaoxing.com" or host.endswith(".chaoxing.com"))
            or parts.username or parts.password or parts.port not in (None, 443)):
        raise RuntimeError("学习广场返回了不受信任的地址")
    return parts


def _number(value, field):
    if value is None or isinstance(value, bool):
        raise RuntimeError(f"学习广场积分缺少有效的 {field}，无法核验")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RuntimeError(f"学习广场积分的 {field} 无效，无法核验") from None
    if not number.is_finite():
        raise RuntimeError(f"学习广场积分的 {field} 无效，无法核验")
    return number


class PlazaClient:
    def __init__(self, session, plaza_url, course, user_id):
        parts = _trusted_url(plaza_url)
        if parts.path.rstrip("/") != "/plaza" or not user_id:
            raise RuntimeError("学习广场地址或登录用户信息无效")
        self.session = session
        self.course_id = str(course["courseId"])
        self.class_id = str(course["clazzId"])
        self.user_id = str(user_id)
        self.base_url = urlunsplit((parts.scheme, parts.netloc, "/plaza/", "", ""))
        query = dict(parse_qsl(parts.query))
        query.update(courseId=self.course_id, classId=self.class_id, userId=self.user_id)
        self.referer = self.base_url + "?" + urlencode(query)
        self.headers = {"X-Requested-With": "XMLHttpRequest", "Referer": self.referer}
        self._chapters = {}
        self._lock = threading.Lock()

    def _json_get(self, path, params):
        try:
            response = self.session.get(urljoin(self.base_url, path), params=params,
                                        headers=self.headers)
            if response.status_code != 200:
                raise RuntimeError(f"学习广场请求失败，HTTP {response.status_code}")
            result = response.json()
        except (requests.RequestException, ValueError):
            raise RuntimeError("学习广场请求失败或返回内容无效") from None
        if not isinstance(result, dict) or result.get("code") != 1 or not isinstance(result.get("data"), dict):
            raise RuntimeError("学习广场未授权或业务请求失败，无法确认进度")
        return result["data"]

    def get_score(self):
        data = self._json_get(f"score/{quote(self.course_id, safe='')}/day-score",
                              {"classId": self.class_id, "userId": self.user_id})
        rule = data.get("rule")
        if not isinstance(rule, dict):
            raise RuntimeError("学习广场未返回积分规则，无法核验")
        score = dict(rule)
        score["totalScore"] = _number(rule.get("totalScore"), "totalScore")
        score["dayScore"] = _number(rule.get("dayScore"), "dayScore")
        return score

    def _open_study(self, url):
        # Follow only trusted HTTPS redirects; never print signed study URLs.
        for _ in range(6):
            _trusted_url(url)
            try:
                response = self.session.get(url, headers={"Referer": self.referer}, allow_redirects=False)
            except requests.RequestException:
                raise RuntimeError("学习广场章节页面请求失败") from None
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise RuntimeError("学习广场章节跳转缺少地址")
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                raise RuntimeError(f"学习广场章节页面请求失败，HTTP {response.status_code}")
            return response.text
        raise RuntimeError("学习广场章节跳转次数过多")

    def enter_chapter(self, knowledge_id):
        knowledge_id = str(knowledge_id)
        with self._lock:
            if knowledge_id in self._chapters:
                return dict(self._chapters[knowledge_id])
            data = self._json_get(
                f"user/{quote(self.course_id, safe='')}/{quote(knowledge_id, safe='')}/modify-node",
                {"classId": self.class_id})
            if data.get("mode") != "study" or not data.get("enc") or not data.get("classId"):
                raise RuntimeError("学习广场未允许正式学习该章节，不能用预览冒充进度")
            domain = data.get("domain", "")
            parts = _trusted_url(domain)
            if parts.path not in ("", "/") or parts.query or parts.fragment:
                raise RuntimeError("学习广场章节域名格式无效")
            origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
            study_url = origin + "/mycourse/studentstudy?" + urlencode({
                "chapterId": knowledge_id, "courseId": self.course_id,
                "clazzid": data["classId"], "enc": data["enc"], "mooc2": 1})
            transfer_url = origin + "/mycourse/transfer?" + urlencode({
                "moocId": self.course_id, "ut": "s", "clazzid": self.class_id, "refer": study_url})
            page = self._open_study(transfer_url)
            match = re.search(r"\butEnc\s*=\s*(['\"])(.*?)\1", page, re.S)
            if match:
                utenc = html.unescape(match.group(2))
            else:
                element = BeautifulSoup(page, "lxml").select_one('input#utEnc, input[name="utEnc"]')
                utenc = element.get("value", "") if element else ""
            if not utenc:
                raise RuntimeError("学习广场章节未取得登录学习上下文，无法确认进度")
            context = {"utenc": utenc, "study_url": study_url}
            self._chapters[knowledge_id] = context
            return dict(context)
