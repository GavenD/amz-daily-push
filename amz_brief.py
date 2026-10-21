# -*- coding: utf-8 -*-
"""
亚马逊美国站每日简报 -> PushPlus 微信推送
仅依赖 Python 标准库，适合 GitHub Actions 定时运行。

环境变量：
  PUSHPLUS_TOKEN  必填，PushPlus 个人 token
  LOOKBACK_DAYS   可选，抓取最近 N 天的文章，默认 3
  DRY_RUN         可选，设为 1 时只打印不推送（本地测试用）
"""
import os
import re
import sys
import json
import time
import html as html_lib
import urllib.request
from datetime import datetime, timedelta

LIST_URL = "https://gs.amazon.cn/news"
BASE = "https://gs.amazon.cn"
HERE = os.path.dirname(os.path.abspath(__file__))
SENT_FILE = os.path.join(HERE, "data", "sent.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

CATEGORY_NAME = {"notices": "政策规则", "brand": "官方资讯", "product": "服务动态"}
# 标题中出现这些站点、且不含“美国”字样的文章视为非美国站内容，直接排除
OTHER_SITE = re.compile(
    r"(欧洲|欧盟|日本|印度|中东|阿联酋|沙特|澳洲|澳大利亚|墨西哥|巴西|新加坡|越南|"
    r"泰国|土耳其|韩国|加拿大|英国|德国|法国|意大利|西班牙|荷兰|瑞典|波兰|比利时|"
    r"爱尔兰|埃及|南非|以色列|马来西亚|菲律宾)"
)
US_KW = re.compile(r"(美国|北美|CPSC|FDA|U\.S\.|美站|US站)")


def log(msg):
    print("[amz-brief] %s" % msg, flush=True)


def fetch(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
        except Exception as e:  # noqa
            last = e
            log("fetch failed(%s/%s) %s: %s" % (i + 1, retries, url, e))
            time.sleep(2)
    raise last


def unescape(t):
    return html_lib.unescape(t)


def strip_tags(frag):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", frag))).strip()


def parse_list(html):
    """从列表页提取文章路径，去重保序。兼容完整 URL 与相对路径。"""
    pattern = (r'(?:href="https?://(?:gs\.amazon\.cn|globalselling\.amazon\.com)|href="")'
               r'(/news/news-(?:notices|brand|product)-\d{6})(?:\?[^"]*)?"')
    paths = re.findall(pattern, html)
    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def path_date(path):
    m = re.search(r"(\d{2})(\d{2})(\d{2})$", path)
    if not m:
        return None
    yy, mm, dd = m.groups()
    return datetime(2000 + int(yy), int(mm), int(dd))


def path_category(path):
    m = re.search(r"news-(notices|brand|product)-", path)
    return CATEGORY_NAME.get(m.group(1), "资讯") if m else "资讯"


# 正文之后的推荐区标志
RECOMMEND_MARK = re.compile(
    r"其他热点资讯|相关推荐|热门文章|推荐阅读|延伸阅读|相关文章|更多资讯|更多精彩"
)


def parse_detail(html):
    m = re.search(r'<meta property="og:title" content="(.*?)"', html, re.S)
    if m:
        title = strip_tags(m.group(1))
    else:
        title = strip_tags(re.search(r"<title>(.*?)</title>", html, re.S).group(1))

    dm = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", html)
    date_str = ("%s-%02d-%02d" % (dm.group(1), int(dm.group(2)), int(dm.group(3)))
                if dm else "")

    # 正文区间：日期行之后 → 推荐区标志之前
    start = dm.end() if dm else 0
    rm = RECOMMEND_MARK.search(html, start)
    end = rm.start() if rm else len(html)
    body = html[start:end]

    paras = []
    for raw in re.findall(r">([^<>]{8,})<", body):
        t = re.sub(r"\s+", " ", unescape(raw)).strip()
        if not re.search(r"[一-鿿]", t):
            continue
        if t.startswith(("var ", "function", "if (", "window.", "*", "{")):
            continue
        paras.append(t)
    return title, date_str, paras


def is_us_related(title, paras):
    if US_KW.search(title):
        return True
    if OTHER_SITE.search(title):
        return False
    # 无明确站点的通用类（FBA、合规、工具等）保留
    if US_KW.search(" ".join(paras[:6])):
        return True
    return not OTHER_SITE.search(title)


SUBTITLE = re.compile(r"^[一二三四五六七八九十]+[、.]|^情况[一二三四五六七八九十]")


def summarize(paras, limit=10, max_len=220):
    out = []
    for p in paras:
        if len(out) >= limit:
            break
        if len(p) < 18 and not SUBTITLE.match(p):
            continue  # 跳过无意义碎片，保留小标题
        out.append(p if len(p) <= max_len else p[:max_len] + "…")
    return out


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_html(items):
    today = datetime.now().strftime("%Y-%m-%d")
    parts = [
        '<div style="font-family:-apple-system,\'Microsoft YaHei\',sans-serif;'
        'color:#232f3e;max-width:680px;margin:0 auto;">',
        '<h2 style="margin:4px 0;">亚马逊美国站每日简报</h2>',
        '<div style="color:#888;font-size:13px;margin-bottom:12px;">%s ｜ 共 %d 条更新</div>'
        % (today, len(items)),
    ]
    for i, it in enumerate(items, 1):
        parts.append(
            '<div style="border:1px solid #e5e5e5;border-radius:10px;padding:14px;'
            'margin-bottom:12px;">'
            '<div style="font-size:12px;color:#fff;background:#ff9900;display:inline-block;'
            'padding:2px 8px;border-radius:10px;margin-bottom:8px;">%s</div>'
            '<a href="%s" style="text-decoration:none;"><h3 style="margin:2px 0 6px;'
            'color:#232f3e;font-size:16px;">%d. %s</h3></a>'
            '<div style="color:#999;font-size:12px;margin-bottom:8px;">%s</div>'
            % (esc(it["cat"]), esc(it["url"]), i, esc(it["title"]), esc(it["date"]))
        )
        for p in it["summary"]:
            parts.append('<p style="margin:6px 0;font-size:14px;line-height:1.7;">%s</p>' % esc(p))
        parts.append('<a href="%s" style="display:inline-block;margin-top:8px;color:#007185;'
                     'font-size:13px;">阅读原文 &gt;</a></div>' % esc(it["url"]))
    parts.append('<div style="color:#aaa;font-size:12px;text-align:center;margin-top:8px;">'
                 '内容来源：亚马逊全球开店官网，仅供参考</div></div>')
    return "".join(parts)


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


def push(token, title, content):
    data = json.dumps({"token": token, "title": title, "content": content,
                       "template": "html"}).encode("utf-8")
    req = urllib.request.Request("https://www.pushplus.plus/send", data=data,
                                 headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
    return resp


def main():
    token = os.environ.get("PUSHPLUS_TOKEN", "")
    dry = os.environ.get("DRY_RUN") == "1"
    lookback = int(os.environ.get("LOOKBACK_DAYS", "3"))
    if not dry and not token:
        log("PUSHPLUS_TOKEN 未设置，退出。")
        sys.exit(1)

    cutoff = datetime.now() - timedelta(days=lookback)
    log("抓取列表页 ...")
    paths = parse_list(fetch(LIST_URL))
    log("列表共发现 %d 篇文章" % len(paths))

    sent = load_sent()
    candidates = [p for p in paths
                  if p not in sent and (path_date(p) is None or path_date(p) >= cutoff)]
    log("最近 %d 天内未推送的候选：%d 篇" % (lookback, len(candidates)))

    items = []
    for p in candidates:
        url = BASE + p
        try:
            title, date_str, paras = parse_detail(fetch(url))
        except Exception as e:  # noqa
            log("详情解析失败，跳过 %s: %s" % (p, e))
            continue
        if not is_us_related(title, paras):
            log("非美国站内容，跳过：%s" % title)
            sent.add(p)
            continue
        items.append({"url": url, "title": title, "date": date_str,
                      "cat": path_category(p), "summary": summarize(paras)})
        sent.add(p)
        time.sleep(1)

    items.sort(key=lambda x: x["date"], reverse=True)
    log("最终简报条数：%d" % len(items))

    if not items:
        log("今日无新内容，不推送。")
        save_sent(sent)
        return

    title = "亚马逊美国站每日简报（%d条）" % len(items)
    html = build_html(items)
    if dry:
        preview = os.path.join(HERE, "preview.html")
        open(preview, "w", encoding="utf-8").write(html)
        log("DRY_RUN 模式，已生成预览：%s" % preview)
        for it in items:
            log("- %s | %s" % (it["date"], it["title"]))
        return

    resp = push(token, title, html)
    log("PushPlus 响应：%s" % json.dumps(resp, ensure_ascii=False))
    if resp.get("code") != 200:
        log("推送失败，不更新已推送记录，下次重试。")
        sys.exit(2)
    save_sent(sent)
    log("完成。")


if __name__ == "__main__":
    main()
