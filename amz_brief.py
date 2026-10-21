# -*- coding: utf-8 -*-
"""
亚马逊美国站每日简报 -> PushPlus 微信推送（双源版）

信息源：
  1. 官方：亚马逊美国站官方论坛「新闻和公告」（英文，自动翻译为中文）
  2. 论坛：知无不言跨境电商社区 RSS（中文，筛选美国站相关）

仅依赖 Python 标准库，适合 GitHub Actions 定时运行。

环境变量：
  PUSHPLUS_TOKEN  必填，PushPlus 个人 token
  LOOKBACK_DAYS   可选，抓取最近 N 天的内容，默认 3
  DRY_RUN         可选，设为 1 时只生成 preview.html 不推送
"""
import os
import re
import sys
import json
import time
import calendar
import html as html_lib
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------- 配置
OFFICIAL_URL = ("https://sellercentral.amazon.com/seller-forums/discussions"
                "?categories%5B%5D=amzn1.spce.category.8b1ad9d2")
OFFICIAL_THREAD = "https://sellercentral.amazon.com/seller-forums/discussions/t/"
FORUM_RSS = "https://www.wearesellers.com/feed"

HERE = os.path.dirname(os.path.abspath(__file__))
SENT_FILE = os.path.join(HERE, "data", "sent.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

MAX_OFFICIAL = 6
MAX_FORUM = 5
WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 会员大促日期（亚马逊每年单独公布，无固定规则，需每年更新；同月起止）
PRIME_SALES = {
    2026: [
        (6, 23, 6, 26, "23–26日 Prime Day 会员日"),
        (10, 6, 10, 7, "6–7日 秋季 Prime 大促"),
    ],
}

# 中文标题中的其他站点词（命中且不含美国相关词则排除）
OTHER_SITE_ZH = re.compile(
    r"(欧洲|欧盟|日本|印度|中东|阿联酋|沙特|澳洲|澳大利亚|墨西哥|巴西|新加坡|越南|"
    r"泰国|土耳其|韩国|加拿大|英国|德国|法国|意大利|西班牙|荷兰|瑞典|波兰|比利时|"
    r"爱尔兰|埃及|南非|以色列|马来西亚|菲律宾|欧洲站|英国站|日本站|德国站|德国本土)"
)
US_KW_ZH = re.compile(r"(美国|北美|美站|美亚|US站|CPSC|FDA|U\.S\.)")
# 英文标题中的其他站点词
OTHER_SITE_EN = re.compile(
    r"\b(UK|U\.K\.|British|European|Europe|Japanese|Japan|Indian|India|Middle East|"
    r"UAE|Saudi|Australian|Australia|Mexico|Mexican|Brazil|Brazilian|Singapore|"
    r"Vietnam|Thai|Thailand|Turkey|Turkish|Korean|Korea|Canada|Canadian|German|"
    r"Germany|France|French|Italian|Italy|Spanish|Spain|Netherlands|Sweden|Poland|"
    r"Polish|Belgium|Belgian|Ireland|Irish|Egypt|Egyptian|South Africa|Israel|"
    r"Israeli|Malaysia|Malaysian|Philippines|Filipino)\b", re.I)
US_KW_EN = re.compile(r"\b(U\.?S\.?A?\.?|United States|American|America|North America)\b", re.I)


def log(msg):
    print("[amz-brief] %s" % msg, flush=True)


# ---------------------------------------------------------------- 抓取
def fetch(url, retries=3, timeout=40):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
        except Exception as e:  # noqa
            last = e
            log("fetch failed(%s/%s) %s: %s" % (i + 1, retries, url[:80], str(e)[:80]))
            time.sleep(2)
    raise last


# ---------------------------------------------------------------- 翻译
def google_translate(text, sl, tl):
    q = urllib.parse.quote(text)
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx"
           "&sl=%s&tl=%s&dt=t&q=%s" % (sl, tl, q))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    arr = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8"))
    return "".join(seg[0] for seg in arr[0])


def mymemory_translate(text, sl, tl):
    q = urllib.parse.quote(text)
    url = ("https://api.mymemory.translated.net/get?q=%s&langpair=%s|%s"
           % (q, sl, tl.replace("zh-CN", "zh-CN")))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
    if d.get("responseStatus") != 200:
        raise RuntimeError("mymemory status %s" % d.get("responseStatus"))
    return d["responseData"]["translatedText"]


_TRANS_BACKENDS = None


def detect_translate_backends():
    """启动时探测一次可用翻译后端，避免每段文本都等不可用接口超时。"""
    global _TRANS_BACKENDS
    backends = []
    for name, fn in (("google", google_translate), ("mymemory", mymemory_translate)):
        try:
            out = fn("hello", "en", "zh-CN")
            if out and out.strip():
                backends.append((name, fn))
                log("翻译后端可用：%s" % name)
        except Exception as e:  # noqa
            log("翻译后端不可用：%s（%s）" % (name, str(e)[:50]))
    _TRANS_BACKENDS = backends


def translate(text, sl="en", tl="zh-CN"):
    """含中文或空文本直接返回；依次尝试可用后端，全失败保留原文。"""
    if not text or not text.strip():
        return text
    if re.search(r"[一-鿿]", text):
        return text
    if _TRANS_BACKENDS is None:
        detect_translate_backends()
    for name, fn in _TRANS_BACKENDS:
        for _ in range(2):
            try:
                out = fn(text, sl, tl)
                if out and out.strip():
                    return out
            except Exception as e:  # noqa
                log("translate failed %s: %s" % (name, str(e)[:70]))
                time.sleep(0.6)
    return text


# ---------------------------------------------------------------- 筛选
def is_us_zh(title):
    if US_KW_ZH.search(title):
        return True
    return not OTHER_SITE_ZH.search(title)


def is_us_en(title):
    if US_KW_EN.search(title):
        return True
    return not OTHER_SITE_EN.search(title)


# ---------------------------------------------------------------- 官方源
def html_to_paras(content_html):
    paras = []
    for m in re.findall(r"<p[^>]*>(.*?)</p>", content_html, re.S):
        t = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", m))).strip()
        if len(t) >= 18:
            paras.append(t)
    if not paras:
        full = re.sub(r"\s+", " ",
                      html_lib.unescape(re.sub(r"<[^>]+>", "", content_html))).strip()
        paras = [full] if full else []
    return paras


def fetch_official(sent, cutoff):
    h = fetch(OFFICIAL_URL)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
    pp = json.loads(m.group(1))["props"]["pageProps"]
    posts = pp.get("threadListings", {}).get("posts", [])
    items = []
    for p in posts:
        key = "off:" + p["threadId"]
        dt = datetime.utcfromtimestamp(p["createdAt"] / 1000)
        if key in sent or dt < cutoff:
            continue
        title_en = re.sub(r"\s+", " ", p.get("title", "")).strip()
        if not title_en or not is_us_en(title_en):
            continue
        paras = html_to_paras(p.get("content", ""))
        items.append({
            "key": key,
            "date": dt.strftime("%Y-%m-%d"),
            "title_en": title_en,
            "paras_en": paras[:3],
            "url": OFFICIAL_THREAD + p["threadId"],
        })
    items = items[:MAX_OFFICIAL]
    return translate_official(items)


def translate_official(items):
    """对官方英文条目逐条翻译标题与摘要。"""
    log("官方候选 %d 篇，开始翻译..." % len(items))
    for it in items:
        it["title"] = translate(it["title_en"])
        it["summary"] = []
        for para in it["paras_en"]:
            para = para if len(para) <= 400 else para[:400] + "…"
            it["summary"].append(translate(para))
            time.sleep(0.2)
        time.sleep(0.3)
    return items


def official_from_sample(path):
    """离线测试：从本地样本 JSON 加载官方条目（跳过网络与时间窗过滤）。"""
    d = json.load(open(path, encoding="utf-8"))
    items = []
    for it in d.get("official", []):
        key = "off:" + it["url"].rstrip("/").rsplit("/", 1)[-1]
        body = it.get("body_en", "")
        paras = [body[:400] + ("…" if len(body) > 400 else "")] if body else []
        items.append({
            "key": key, "date": it["date"], "title_en": it["title_en"],
            "paras_en": paras, "url": it["url"],
        })
    return translate_official(items[:MAX_OFFICIAL])


def forum_from_sample(path):
    """离线测试：从本地样本 JSON 加载论坛条目（应用美国站筛选，不做时间窗过滤）。"""
    d = json.load(open(path, encoding="utf-8"))
    items = []
    for it in d.get("forum", []):
        title = html_lib.unescape(it["title"].strip())
        if not is_us_zh(title):
            continue
        body = html_lib.unescape(re.sub(r"<[^>]+>", "", it.get("body", "")))
        body = re.sub(r"\s+", " ", body).strip()
        body = body[:220] + ("…" if len(body) > 220 else "")
        items.append({
            "key": "wsy:" + it["url"].rstrip("/").rsplit("/", 1)[-1],
            "date": it["date"], "title": title, "summary": body, "url": it["url"],
        })
    return items[:MAX_FORUM]


# ---------------------------------------------------------------- 论坛源
def fetch_forum(sent, cutoff):
    raw = fetch(FORUM_RSS)
    root = ET.fromstring(raw)
    items = []
    for node in root.findall(".//item"):
        link = node.findtext("link") or ""
        qid = link.rstrip("/").rsplit("/", 1)[-1]
        key = "wsy:" + qid
        try:
            pub = parsedate_to_datetime(node.findtext("pubDate"))
            dt = pub.replace(tzinfo=None)
        except Exception:
            continue
        if key in sent or dt < cutoff:
            continue
        title = html_lib.unescape(node.findtext("title") or "").strip()
        if not title or not is_us_zh(title):
            continue
        desc = node.findtext("description") or ""
        body = re.sub(r"\s+", " ",
                      html_lib.unescape(re.sub(r"<[^>]+>", "", desc))).strip()
        body = body[:220] + ("…" if len(body) > 220 else "")
        items.append({
            "key": key,
            "date": dt.strftime("%Y-%m-%d"),
            "title": title,
            "summary": body,
            "url": link,
        })
    return items[:MAX_FORUM]


# ---------------------------------------------------------------- 日期 / 日历
def nth_weekday(year, month, weekday, n):
    """weekday: Monday=0 ... Sunday=6；n=第几个"""
    days = [d for d in calendar.Calendar().itermonthdates(year, month)
            if d.month == month and d.weekday() == weekday]
    return days[n - 1]


def last_weekday(year, month, weekday):
    days = [d for d in calendar.Calendar().itermonthdates(year, month)
            if d.month == month and d.weekday() == weekday]
    return days[-1]


def key_dates(year):
    thanks = nth_weekday(year, 11, 3, 4)  # 感恩节：11 月第四个周四
    return {
        "presidents": nth_weekday(year, 2, 0, 3),
        "mothers": nth_weekday(year, 5, 6, 2),
        "memorial": last_weekday(year, 5, 0),
        "juneteenth": date(year, 6, 19),
        "fathers": nth_weekday(year, 6, 6, 3),
        "labor": nth_weekday(year, 9, 0, 1),
        "columbus": nth_weekday(year, 10, 0, 2),
        "thanksgiving": thanks,
        "black_friday": thanks + timedelta(days=1),
        "cyber_monday": thanks + timedelta(days=4),
    }


def build_calendar(year):
    """返回 {month: [(day, name, kind)]}，kind: holiday / sale"""
    kd = key_dates(year)
    events = {m: [] for m in range(1, 13)}

    def add(d, name, kind):
        if d.year == year:
            events[d.month].append((d.day, name, kind))

    fixed = [
        (1, 1, "元旦"), (2, 14, "情人节"), (3, 17, "圣帕特里克节"),
        (4, 5, "复活节"), (4, 22, "地球日"), (7, 4, "独立日"),
        (10, 31, "万圣节"), (11, 11, "退伍军人节"),
        (12, 24, "平安夜"), (12, 25, "圣诞节"), (12, 26, "Kwanzaa"),
    ]
    for m, d, name in fixed:
        add(date(year, m, d), name, "holiday")
    add(kd["presidents"], "总统日", "holiday")
    add(kd["mothers"], "母亲节", "holiday")
    add(kd["memorial"], "阵亡将士纪念日", "holiday")
    add(kd["juneteenth"], "Juneteenth", "holiday")
    add(kd["fathers"], "父亲节", "holiday")
    add(date(year, 8, 1), "返校季", "holiday")
    add(kd["labor"], "劳动节", "holiday")
    add(kd["columbus"], "哥伦布日", "holiday")
    add(kd["thanksgiving"], "感恩节", "holiday")
    add(kd["black_friday"], "黑五", "sale")
    add(kd["cyber_monday"], "网一", "sale")
    # 会员大促（配置；name 已含日期区间，渲染时不再重复加“X日”）
    for sm, sd, em, ed, name in PRIME_SALES.get(year, []):
        events[sm].append((sd, name, "sale"))

    for m in events:
        events[m].sort(key=lambda x: x[0])
    return events


def countdown_cards(today):
    """近期大促倒计时（未来 0~90 天内最近的 3 个）。"""
    kd = key_dates(today.year)
    targets = [
        ("黑五", kd["black_friday"]),
        ("网一", kd["cyber_monday"]),
        ("圣诞节", date(today.year, 12, 25)),
        ("Prime Day",
         date(today.year, PRIME_SALES[today.year][0][0], PRIME_SALES[today.year][0][1])
         if today.year in PRIME_SALES else None),
    ]
    cards = []
    for name, d in targets:
        if d is None:
            continue
        delta = (d - today).days
        if 0 <= delta <= 90:
            cards.append((name, delta, "%d/%d" % (d.month, d.day)))
    cards.sort(key=lambda x: x[1])
    return cards[:3]


# ---------------------------------------------------------------- 去重
def load_sent():
    if os.path.exists(SENT_FILE):
        try:
            return set(json.load(open(SENT_FILE, encoding="utf-8")).get("sent", []))
        except Exception:
            return set()
    return set()


def save_sent(sent):
    os.makedirs(os.path.dirname(SENT_FILE), exist_ok=True)
    json.dump({"sent": sorted(sent)}, open(SENT_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- 渲染
def article_card(tag, tag_color, title, date_str, summary, url):
    if isinstance(summary, str):
        paras = [summary]
    else:
        paras = summary
    parts = [
        '<div style="margin:0 18px 12px;border:1px solid #ececec;border-radius:10px;padding:13px;">',
        '<span style="font-size:11px;color:#fff;background:%s;padding:2px 8px;border-radius:10px;">%s</span>'
        % (tag_color, tag),
        '<div style="font-size:15px;font-weight:600;color:#232f3e;margin:8px 0 4px;line-height:1.4;">%s</div>'
        % esc(title),
        '<div style="color:#aaa;font-size:12px;margin-bottom:8px;">%s</div>' % esc(date_str),
    ]
    for p in paras:
        if p:
            parts.append('<p style="margin:6px 0;font-size:13px;line-height:1.7;color:#444;">%s</p>' % esc(p))
    parts.append('<a href="%s" style="color:#007185;font-size:12px;text-decoration:none;">阅读原文 ›</a></div>'
                 % esc(url))
    return "".join(parts)


def section_title(text, color):
    return ('<div style="padding:14px 18px 0;"><div style="font-size:14px;font-weight:700;'
            'color:%s;border-left:3px solid %s;padding-left:8px;margin-bottom:12px;">%s</div></div>'
            % (color, color, esc(text)))


def render_calendar(today):
    year = today.year
    events = build_calendar(year)
    parts = ['<div style="padding:16px 18px 4px;">',
             '<div style="font-size:14px;font-weight:700;color:#232f3e;border-left:3px solid #232f3e;'
             'padding-left:8px;margin-bottom:12px;">%d 全年大促 · 节日日历</div>' % year,
             '<div style="display:flex;flex-wrap:wrap;gap:7px;">']
    for m in range(1, 13):
        if m < today.month:
            state = "past"
        elif m == today.month:
            state = "current"
        else:
            state = "future"
        if state == "past":
            style = "border:1px solid #eee;opacity:0.45;"
            mcolor = "#666"
        elif state == "current":
            style = "border:1px solid #2563eb;background:#f5f8ff;"
            mcolor = "#2563eb"
        else:
            style = "border:1px solid #ffe2b8;background:#fff8ef;"
            mcolor = "#b06a00"
        parts.append('<div style="width:calc(50%% - 4px);box-sizing:border-box;'
                     'border-radius:8px;padding:8px;%s">' % style)
        mlabel = "%d月" % m + (" · 当月" if state == "current" else "")
        parts.append('<div style="font-size:12px;font-weight:700;color:%s;margin-bottom:5px;">%s</div>'
                     % (mcolor, mlabel))
        lines = []
        for day, name, kind in events[m]:
            # 跨天活动的 label 已含日期（以数字开头），不再重复加“X日”
            prefix = "" if re.match(r"^\d", name) else "%d日 " % day
            if kind == "sale":
                lines.append('<span style="color:#ff9900;font-weight:700;">%s%s</span>'
                             % (prefix, esc(name)))
            else:
                lines.append("%s%s" % (prefix, esc(name)))
        parts.append('<div style="font-size:11px;color:%s;line-height:1.7;">%s</div>'
                     % ("#555" if state != "past" else "#888", "<br>".join(lines)))
        parts.append('</div>')
    parts.append('</div></div>')
    return "".join(parts)


def build_html(official, forum, today):
    if hasattr(today, "date"):
        today = today.date()
    wkd = WEEKDAY_CN[today.weekday()]
    head = (
        f'<div style="display:flex;align-items:center;justify-content:space-between;'
        f'padding:16px 18px;border-bottom:1px solid #f2f2f2;">'
        f'<div style="display:flex;align-items:center;">'
        f'<span style="width:9px;height:9px;border-radius:50%;background:#2563eb;'
        f'display:inline-block;margin-right:10px;"></span>'
        f'<span style="font-size:18px;color:#232f3e;">'
        f'<b style="background:#eef3ff;padding:2px 6px;border-radius:5px;">{today.month}</b> 月 '
        f'<b style="background:#eef3ff;padding:2px 6px;border-radius:5px;">{today.day}</b> 日 · {wkd}'
        f'</span></div>'
        f'<span style="color:#2563eb;font-size:15px;font-weight:600;">今日</span></div>'
    )

    cards = countdown_cards(today)
    cd = ""
    if cards:
        boxes = []
        for name, delta, md in cards:
            boxes.append(
                f'<div style="flex:1;border:1px solid #ffe2b8;background:#fff8ef;border-radius:9px;'
                f'padding:10px;text-align:center;">'
                f'<div style="font-size:12px;color:#b06a00;">{name}</div>'
                f'<div style="font-size:20px;font-weight:700;color:#ff9900;line-height:1.3;">{delta}</div>'
                f'<div style="font-size:11px;color:#999;">天后 · {md}</div></div>')
        cd = ('<div style="padding:14px 18px 6px;">'
              '<div style="font-size:13px;font-weight:700;color:#232f3e;margin-bottom:10px;">近期大促节点</div>'
              '<div style="display:flex;gap:8px;">%s</div></div>' % "".join(boxes))

    total = len(official) + len(forum)
    if total == 0:
        sub = "今日无新内容"
    else:
        sub = "官方公告 %d 条 · 论坛精选 %d 条" % (len(official), len(forum))
    title_head = (
        '<div style="padding:12px 18px 0;">'
        '<div style="font-size:19px;font-weight:700;color:#232f3e;">亚马逊美国站每日简报</div>'
        '<div style="color:#999;font-size:13px;margin-top:6px;">%s</div></div>' % sub)

    body = []
    if official:
        body.append(section_title("官方公告", "#ff9900"))
        for it in official:
            body.append(article_card("官方", "#ff9900", it["title"], it["date"],
                                     it["summary"], it["url"]))
    if forum:
        body.append(section_title("卖家论坛精选 · 知无不言", "#007185"))
        for it in forum:
            body.append(article_card("论坛", "#007185", it["title"], it["date"],
                                     it["summary"], it["url"]))
    if total == 0:
        body.append('<div style="padding:18px;color:#999;font-size:13px;">'
                    '今天两个信息源暂未发现新内容，全年大促日历见下方。</div>')

    tail = ('<div style="color:#bbb;font-size:11px;text-align:center;padding:14px 18px 18px;">'
            '内容来源：亚马逊美国站官方论坛 · 知无不言社区，仅供参考</div>')

    return (
        '<div style="font-family:-apple-system,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
        'color:#232f3e;">'
        + head + cd + title_head + "".join(body) + render_calendar(today) + tail + "</div>"
    )


# ---------------------------------------------------------------- 推送
def push(token, title, content):
    data = json.dumps({"token": token, "title": title, "content": content,
                       "template": "html"}).encode("utf-8")
    req = urllib.request.Request("https://www.pushplus.plus/send", data=data,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))


def main():
    token = os.environ.get("PUSHPLUS_TOKEN", "")
    dry = os.environ.get("DRY_RUN") == "1"
    lookback = int(os.environ.get("LOOKBACK_DAYS", "3"))
    if not dry and not token:
        log("PUSHPLUS_TOKEN 未设置，退出。")
        sys.exit(1)

    today = datetime.now()
    cutoff = today - timedelta(days=lookback)
    sent = load_sent()

    sample_file = os.environ.get("SAMPLE_FILE")
    if sample_file:
        # 离线测试：直接从本地样本读取，不访问网络、不做去重
        log("离线样本模式：%s" % sample_file)
        detect_translate_backends()
        official = official_from_sample(sample_file)
        forum = forum_from_sample(sample_file)
    else:
        detect_translate_backends()
        # 单源容错：一个源失败不影响另一个源
        try:
            log("抓取官方公告 ...")
            official = fetch_official(sent, cutoff)
        except Exception as e:  # noqa
            log("官方源抓取失败，本次跳过：%s" % str(e)[:100])
            official = []
        try:
            log("抓取知无不言 RSS ...")
            forum = fetch_forum(sent, cutoff)
        except Exception as e:  # noqa
            log("论坛源抓取失败，本次跳过：%s" % str(e)[:100])
            forum = []
    log("官方简报 %d 条 · 论坛简报 %d 条" % (len(official), len(forum)))

    total = len(official) + len(forum)
    html = build_html(official, forum, today)

    if dry:
        preview = os.path.join(HERE, "preview.html")
        open(preview, "w", encoding="utf-8").write(
            '<html><body style="background:#ededed;margin:0;padding:16px 0;">'
            '<div style="max-width:420px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;">'
            + html + '</div></body></html>')
        log("DRY_RUN 已生成预览：%s（官方 %d，论坛 %d）" % (preview, len(official), len(forum)))
        for it in official:
            log("[官方] %s | %s" % (it["date"], it["title"]))
        for it in forum:
            log("[论坛] %s | %s" % (it["date"], it["title"]))
        return

    if total == 0:
        log("今日无新内容，不推送。")
        save_sent(sent)
        return

    title = "亚马逊美国站每日简报（官方%d·论坛%d）" % (len(official), len(forum))
    resp = push(token, title, html)
    log("PushPlus 响应：%s" % json.dumps(resp, ensure_ascii=False))
    if resp.get("code") != 200:
        log("推送失败，不更新已推送记录，下次重试。")
        sys.exit(2)
    for it in official + forum:
        sent.add(it["key"])
    save_sent(sent)
    log("完成。")


if __name__ == "__main__":
    main()
