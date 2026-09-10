#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pre-doc / RA 招聘公告监控与邮件推送。

监控三个来源，与上次快照比对，把新增岗位通过 Gmail 推送到邮箱。
详见同目录 README.md。
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import logging
import os
import re
import smtplib
import socket
import sys
import time
from datetime import date, datetime
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
EXAMPLE_PATH = BASE_DIR / "config.example.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_DIR = BASE_DIR / "logs"
EXTRA_CA_DIR = BASE_DIR / "extra_ca"
CA_BUNDLE_PATH = BASE_DIR / ".ca_bundle.pem"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

log = logging.getLogger("watch_jobs")


class ParseError(Exception):
    """页面结构与预期不符 —— 视为抓取失败，绝不当成『0 条岗位』。"""


# --------------------------------------------------------------------------
# 文本工具
# --------------------------------------------------------------------------

def norm_space(s):
    """压缩空白、去掉 &nbsp;。"""
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def norm_key(s):
    """用于构造去重主键的规范化：小写 + 压空白 + 去首尾标点。"""
    return norm_space(s).lower().strip(" .,;:-_/|")


def norm_label(s):
    """标签模糊匹配用：只留小写字母数字。

    这样 'Field(s) of Research'、'Fields of Research'、'Field(s0 of research'
    （站方错字）、'NBER Sponsoring Researcher)s)' 都能被同一条规则命中。
    """
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# 标签 -> 字段名。按顺序匹配，先命中先用。
LABEL_RULES = (
    ("deadline", "deadline"),
    ("researcher", "researchers"),
    ("institution", "institution"),
    ("field", "fields"),
    ("location", "location"),
    ("startdate", "start_date"),
    ("visa", "visa"),
)

FIELD_LABELS_CN = (
    ("institution", "机构"),
    ("researchers", "导师"),
    ("fields", "研究领域"),
    ("deadline", "Deadline"),
    ("location", "地点"),
    ("start_date", "开始时间"),
    ("visa", "签证"),
)


# 标签短语（容忍站方的各种写法与错字：Field(s0 of research、Researcher)s) 等）
_LABEL_PHRASE = (
    r"(?:nber[\s\-]*)?sponsoring[\s\-]*researchers?\s*[\(\)s0-9]{0,5}"
    r"|(?:nber[\s\-]*)?sponsoring[\s\-]*institutions?"
    r"|institutions?"
    r"|fields?\s*[\(\)s0-9]{0,5}\s*of\s+research"
    r"|fields?\s*\(\s*s[0-9\)]*\)?"
    r"|deadlines?"
    r"|start\s+date"
)

# 站方偶尔漏掉 <br>，两个字段挤在一行：
# 'Institution: West Point Field(s) of research: Labor, Public, ...'
# 这里在行内出现的「标签 + 冒号」处切开。要求带冒号，避免误伤正常取值。
RE_EMBEDDED_LABEL = re.compile(r"(?i)\s+(?=(?:%s)\s*[:：])" % _LABEL_PHRASE)

# 站方偶尔漏掉冒号：'NBER Sponsoring Researcher(s) Christopher Snyder, Michael Kremer'
# 只在行首匹配，且标签后必须还有内容。
RE_LABEL_NO_COLON = re.compile(r"(?i)^\s*(%s)\s+(?=\S)" % _LABEL_PHRASE)


def split_embedded(value):
    """取值里如果又冒出一个『标签:』，从那里切开，返回 (真正的取值, 剩下的部分)。

    只对已经识别出字段的取值做这一步 —— 若对整行做，'NBER Sponsoring Researcher(s): X'
    会从 'NBER ' 后面被切开，反而把 'NBER' 变成孤立碎片。
    """
    parts = RE_EMBEDDED_LABEL.split(value, maxsplit=1)
    if len(parts) == 2:
        return norm_space(parts[0]), parts[1].strip()
    return value, None


def _match_field(label_text):
    n = norm_label(label_text)
    for needle, field in LABEL_RULES:
        if needle in n:
            return field
    return None


def split_label(line):
    """把 'Institution: MIT' 拆成 ('institution', 'MIT')；认不出则返回 (None, line)。"""
    m = re.match(r"\s*([^:：]{1,60}?)\s*[:：]\s*(.*)$", line, re.S)
    if m:
        field = _match_field(m.group(1))
        if field:
            return field, norm_space(m.group(2))

    # 退一步：标签后漏了冒号
    m = RE_LABEL_NO_COLON.match(line)
    if m:
        field = _match_field(m.group(1))
        if field:
            return field, norm_space(line[m.end():])
    return None, line


def block_text(node):
    """把一个节点取成按 <br> 分行的纯文本。

    这里必须用无分隔符的 get_text()：若用 get_text("\\n")，BeautifulSoup 会在每两个
    内联子节点之间都插入换行，于是 '<strong>Deadline</strong>: Rolling' 会被拆成
    'Deadline' 和 ': Rolling' 两行，字段就丢了。换行只应来自真正的 <br>。
    """
    if node is None:
        return ""
    for br in node.find_all("br"):
        br.replace_with("\n")
    raw = node.get_text().replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def parse_meta_lines(lines):
    """把若干行元信息解析成字段字典；认不出的行收进 notes 而不是丢弃。"""
    out = {}
    notes = []
    pending = None  # 上一行只有标签没有值，等下一行补上
    queue = list(lines)
    while queue:
        ln = queue.pop(0)
        if not ln.strip():
            continue
        field, value = split_label(ln)
        if field:
            # 站方偶尔漏 <br>，两个字段挤在一行；把后半段放回队列重新处理。
            value, tail = split_embedded(value)
            if tail:
                queue.insert(0, tail)
            if value:
                out.setdefault(field, value)
                pending = None
            else:
                pending = field
        elif pending:
            out.setdefault(pending, norm_space(ln))
            pending = None
        else:
            notes.append(norm_space(ln))
    if notes:
        out["notes"] = " / ".join(notes)
    return out


# --------------------------------------------------------------------------
# 解析器
# --------------------------------------------------------------------------

def parse_predoc(page_html, page_url):
    """predoc.org —— Sitecore 服务端渲染，每条岗位是一个 <article class="... sorted ...">。"""
    soup = BeautifulSoup(page_html, "html.parser")

    # 分类标签的显示名直接从页面自带的筛选下拉框里读，站方加了新领域也不会漏。
    taxonomy = {}
    for sel in soup.find_all("select", attrs={"data-filter-group": True}):
        for opt in sel.find_all("option"):
            value = (opt.get("value") or "").lstrip(".").strip()
            label = norm_space(opt.get_text())
            if value and value != "all":
                taxonomy[value] = label

    articles = [a for a in soup.find_all("article") if "sorted" in (a.get("class") or [])]
    if not articles:
        raise ParseError("未找到 article.sorted —— 页面结构可能已改版")

    items = []
    for art in articles:
        h2 = art.find("h2")
        anchor = h2.find("a", href=True) if h2 else None
        if not anchor:
            continue
        link = urljoin(page_url, anchor["href"].strip())
        title = norm_space(anchor.get_text())
        if not title:
            continue

        para = art.select_one(".swiss-text p") or art.find("p")
        raw_meta = block_text(para)
        item = parse_meta_lines(raw_meta.split("\n")) if raw_meta else {}
        item.update(title=title, link=link, raw_meta=raw_meta)

        classes = [c for c in (art.get("class") or []) if c not in ("all", "sorted")]
        item["tags"] = [taxonomy.get(c, c) for c in classes]
        items.append(item)
    return items


def parse_nber(page_html, page_url):
    """两个 NBER 页 —— Drupal 富文本正文，岗位是 'Available Positions' 之后的一串 <p>。"""
    soup = BeautifulSoup(page_html, "html.parser")
    root = soup.find("main") or soup

    heading = None
    for tag in root.find_all(["h1", "h2", "h3", "h4"]):
        if "availableposition" in norm_label(tag.get_text()):
            heading = tag
            break
    if heading is None:
        raise ParseError("未找到 'Available Positions' 标题 —— 页面结构可能已改版")

    items = []
    for para in heading.find_all_next("p"):
        anchors = [
            a for a in para.find_all("a", href=True)
            if not a["href"].strip().lower().startswith("mailto:")
        ]
        if not anchors:
            continue

        raw_meta = block_text(para)
        # 必须看到 Institution 标签才算一条岗位，否则是正文说明段落。
        if not any(split_label(ln)[0] == "institution" for ln in raw_meta.split("\n")):
            continue

        # href 可能带前导空格，也可能是 /sites/... 相对路径。
        link = urljoin(page_url, anchors[-1]["href"].strip())

        lines = raw_meta.split("\n")
        title = norm_space(lines[0])
        if not title:
            continue
        # 'Link for Job Posting' 这类纯链接文字不算元信息。
        link_texts = set(norm_key(a.get_text()) for a in anchors)
        rest = [ln for ln in lines[1:] if norm_key(ln) not in link_texts]

        item = parse_meta_lines(rest)
        item.update(title=title, link=link, raw_meta=raw_meta, tags=[])
        items.append(item)
    return items


SOURCES = [
    {
        "id": "predoc",
        "name": "predoc.org",
        "url": "https://www.predoc.org/opportunities",
        "parser": parse_predoc,
        "min_count": 20,
    },
    {
        "id": "nber",
        "name": "NBER（本部）",
        "url": "https://www.nber.org/career-resources/research-assistant-positions-nber",
        "parser": parse_nber,
        "min_count": 0,  # 该页常年可能是 0 条，空是合法状态
    },
    {
        "id": "nber_external",
        "name": "NBER（非本部）",
        "url": "https://www.nber.org/career-resources/research-assistant-positions-not-nber",
        "parser": parse_nber,
        "min_count": 30,
    },
]
SOURCE_BY_ID = dict((s["id"], s) for s in SOURCES)


_ca_bundle_cache = []


def ca_bundle():
    """certifi 根证书 + extra_ca/ 下补充的中间证书，合成一份 CA bundle。

    predoc.org 的服务器只发了叶子证书、漏发了 GoDaddy 的中间证书。浏览器和 curl
    能打开是因为 Windows 会按证书里的 AIA 字段自动补下载中间证书，而 OpenSSL/Python
    不做这件事，于是报 CERTIFICATE_VERIFY_FAILED。正确解法是把那张中间证书随项目带上，
    而不是关掉证书校验 —— 校验始终是开着的。
    """
    if _ca_bundle_cache:
        return _ca_bundle_cache[0]

    extras = sorted(EXTRA_CA_DIR.glob("*.pem")) if EXTRA_CA_DIR.is_dir() else []
    if not extras:
        _ca_bundle_cache.append(True)
        return True  # 交给 requests 用默认的 certifi

    try:
        import certifi
        chunks = [Path(certifi.where()).read_text(encoding="utf-8")]
    except Exception:
        chunks = []
    for pem in extras:
        chunks.append(pem.read_text(encoding="utf-8"))
    CA_BUNDLE_PATH.write_text("\n".join(chunks), encoding="utf-8")
    log.debug("CA bundle 已合成，附加 %d 张证书", len(extras))
    _ca_bundle_cache.append(str(CA_BUNDLE_PATH))
    return str(CA_BUNDLE_PATH)


def wait_for_network(hosts, budget=300, log_every=60):
    """开跑前先等 DNS 能解析，最多等 budget 秒。

    计划任务开了 StartWhenAvailable，错过的那次会在开机/唤醒后立刻补跑 ——
    而那正是 Wi-Fi 刚开始重连、DNS 还没就绪的时刻。之前每个来源各自重试
    几秒就放弃，于是排在前面的站必然失败。这里改成先整体等网络。

    返回 True 表示网络已就绪；False 表示等超时了（照常往下跑，让各来源
    自己的重试和护栏去处理，不能因为探测失败就整轮不干活）。
    """
    start = time.monotonic()
    announced = False
    while True:
        for host in hosts:
            try:
                socket.getaddrinfo(host, 443)
                if announced:
                    log.info("网络已就绪（等待 %.0f 秒）", time.monotonic() - start)
                return True
            except socket.gaierror:
                continue
        waited = time.monotonic() - start
        if waited >= budget:
            log.warning("等待网络 %.0f 秒仍无法解析域名，继续尝试抓取", waited)
            return False
        if not announced:
            log.info("域名暂时解析不了，等待网络就绪（最多 %d 秒）", budget)
            announced = True
        elif int(waited) % log_every < 5:
            log.info("仍在等待网络……已等 %.0f 秒", waited)
        time.sleep(5)


def fetch(url, retries=5, timeout=30):
    last = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                timeout=timeout,
                verify=ca_bundle(),
            )
            resp.raise_for_status()
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except Exception as exc:
            last = exc
            if attempt < retries - 1:
                # 退避 2/4/8/16 秒。原来 3 次共 6 秒就放弃，对刚唤醒、
                # Wi-Fi 还在重连的笔记本太短了。
                wait = 2 ** attempt * 2
                log.warning("抓取失败（第 %d 次）：%s；%d 秒后重试", attempt + 1, exc, wait)
                time.sleep(wait)
    raise last


def collect(source):
    """抓取并解析单个来源，返回 {ok, items, error}。任一来源失败不影响其他来源。"""
    name = source["name"]
    try:
        page = fetch(source["url"])
        items = source["parser"](page, source["url"])
        if len(items) < source["min_count"]:
            raise ParseError(
                "只解析出 %d 条，低于下限 %d 条 —— 疑似解析异常"
                % (len(items), source["min_count"])
            )
        log.info("[%s] 解析到 %d 条", name, len(items))
        return {"ok": True, "items": items, "error": None}
    except Exception as exc:
        log.error("[%s] 抓取/解析失败：%s", name, exc)
        return {"ok": False, "items": [], "error": str(exc)}


def item_key(source_id, item):
    """复合主键：来源 + 链接 + 标题 + 机构 + 导师。

    NBER 页面上有 11 个链接被多条不同岗位共用（同一个招聘汇总页挂多个职位），
    所以不能只用 link 去重。导师也必须进主键 —— Wharton 那条 'Predoctoral Research
    Analyst' 就是同一链接、同一标题、同一机构下的两个不同职位，只有导师不同
    （Lockwood & Rees-Jones / Kessler & Low），漏掉导师会把其中一个永久藏起来。

    刻意不含 fields：那是最容易被站方随手改动的字段，进主键会导致改一次文案就
    误报一条『新岗位』。
    """
    raw = "|".join([
        source_id,
        item.get("link", ""),
        norm_key(item.get("title", "")),
        norm_key(item.get("institution", "")),
        norm_key(item.get("researchers", "")),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 配置与状态
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "smtp": {
        "host": "smtp.gmail.com",
        "port": 465,
        "user": "you@gmail.com",
        "app_password": "",
        "to": "you@gmail.com",
    },
    "sources": {"predoc": True, "nber": True, "nber_external": True},
    "daily_email_even_if_empty": True,
    "verification_email_full_list": True,
}


def load_config():
    if not EXAMPLE_PATH.exists():
        EXAMPLE_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.warning("已生成 config.json，请填入 Gmail 应用专用密码后再运行")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in cfg.items():
        if k != "smtp":
            merged[k] = v
    merged["smtp"].update(cfg.get("smtp", {}))
    return merged


def empty_state():
    return {
        "version": 1,
        "meta": {
            "initialized": False,
            "email_verified": False,
            "smtp_fingerprint": "",
            "last_new_at": None,
            "last_run_at": None,
            "failures": {},
        },
        "sources": {},
    }


def load_state():
    if not STATE_PATH.exists():
        return empty_state()
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("state.json 损坏（%s），按首次运行处理", exc)
        return empty_state()
    base = empty_state()
    base["meta"].update(state.get("meta", {}))
    base["sources"].update(state.get("sources", {}))
    return base


def save_state(state):
    """原子写入：先落临时文件再替换，断电不会留下半截 JSON。"""
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def smtp_fingerprint(smtp):
    raw = "%s->%s" % (smtp.get("user", ""), smtp.get("to", ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 邮件渲染
# --------------------------------------------------------------------------

def esc(s):
    return html_mod.escape(s or "", quote=True)


CSS_CARD = (
    "border:1px solid #e2e5ea;border-left:4px solid #2f6fdb;border-radius:6px;"
    "padding:12px 14px;margin:0 0 12px 0;background:#ffffff;"
)
CSS_BADGE = (
    "display:inline-block;background:#eef3fc;color:#2f6fdb;border-radius:3px;"
    "padding:1px 7px;font-size:12px;margin-left:6px;vertical-align:middle;"
)
CSS_LABEL = "color:#6b7280;font-size:13px;padding:2px 10px 2px 0;vertical-align:top;white-space:nowrap;"
CSS_VALUE = "color:#111827;font-size:13px;padding:2px 0;vertical-align:top;"


def card_html(item, source_name):
    rows = []
    for key, label in FIELD_LABELS_CN:
        value = item.get(key)
        if not value:
            continue
        style = CSS_VALUE + ("font-weight:700;" if key == "deadline" else "")
        rows.append(
            '<tr><td style="%s">%s</td><td style="%s">%s</td></tr>'
            % (CSS_LABEL, esc(label), style, esc(value))
        )
    if not rows and item.get("raw_meta"):
        rows.append(
            '<tr><td style="%s">详情</td><td style="%s">%s</td></tr>'
            % (CSS_LABEL, CSS_VALUE, esc(item["raw_meta"]).replace("\n", "<br>"))
        )

    tags = ""
    if item.get("tags"):
        tags = (
            '<div style="margin-top:8px;color:#9099a8;font-size:12px;">%s</div>'
            % esc(" · ".join(item["tags"]))
        )

    return (
        '<div style="%s">'
        '<div style="font-size:15px;font-weight:600;line-height:1.4;">'
        '<a href="%s" style="color:#1a4fa0;text-decoration:none;">%s</a>'
        '<span style="%s">%s</span></div>'
        '<table style="border-collapse:collapse;margin-top:8px;">%s</table>%s</div>'
    ) % (
        CSS_CARD,
        esc(item.get("link", "")),
        esc(item.get("title", "(无标题)")),
        CSS_BADGE,
        esc(source_name),
        "".join(rows),
        tags,
    )


def card_text(item, source_name):
    lines = ["* %s  [%s]" % (item.get("title", "(无标题)"), source_name)]
    for key, label in FIELD_LABELS_CN:
        if item.get(key):
            lines.append("  %s: %s" % (label, item[key]))
    if len(lines) == 1 and item.get("raw_meta"):
        for ln in item["raw_meta"].split("\n")[1:]:
            lines.append("  " + ln)
    lines.append("  " + item.get("link", ""))
    return "\n".join(lines)


def page_shell(body_html):
    return (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,'
        'Microsoft YaHei,sans-serif;background:#f5f6f8;padding:18px;">'
        '<div style="max-width:680px;margin:0 auto;">%s</div></div>' % body_html
    )


def h_section(text):
    return (
        '<h2 style="font-size:14px;color:#374151;margin:20px 0 10px 0;'
        'padding-bottom:6px;border-bottom:1px solid #dfe3e8;">%s</h2>' % esc(text)
    )


def counts_block(counts, failed_sids):
    """页脚：各来源在招数，失败的来源标出来。"""
    rows = []
    total = 0
    for src in SOURCES:
        sid = src["id"]
        if sid in failed_sids:
            value = "抓取失败"
        elif sid in counts:
            value = "%d 个在招" % counts[sid]
            total += counts[sid]
        else:
            continue
        rows.append(
            '<tr><td style="%s">%s</td><td style="%s">%s</td></tr>'
            % (CSS_LABEL, esc(src["name"]), CSS_VALUE, esc(value))
        )
    rows.append(
        '<tr><td style="%s">合计</td><td style="%s">%d 个在招</td></tr>'
        % (CSS_LABEL, CSS_VALUE + "font-weight:700;", total)
    )
    return (
        '<div style="background:#ffffff;border:1px solid #e2e5ea;border-radius:6px;'
        'padding:12px 14px;margin-top:8px;">'
        '<table style="border-collapse:collapse;">%s</table></div>' % "".join(rows)
    )


def counts_text(counts, failed_sids):
    lines = []
    total = 0
    for src in SOURCES:
        sid = src["id"]
        if sid in failed_sids:
            lines.append("  %s: 抓取失败" % src["name"])
        elif sid in counts:
            lines.append("  %s: %d 个在招" % (src["name"], counts[sid]))
            total += counts[sid]
    lines.append("  合计: %d 个在招" % total)
    return "\n".join(lines)


def failure_banner_html(failures):
    if not failures:
        return ""
    rows = "".join(
        "<li>%s —— %s（已连续失败 %d 次）</li>" % (esc(name), esc(err), n)
        for _sid, name, err, n in failures
    )
    return (
        '<div style="background:#fff5f5;border:1px solid #f3c2c2;border-left:4px solid #d94848;'
        'border-radius:6px;padding:12px 14px;margin:0 0 14px 0;color:#8a2b2b;font-size:13px;">'
        '<strong>抓取异常</strong><ul style="margin:8px 0 0 18px;padding:0;">%s</ul></div>' % rows
    )


def failure_banner_text(failures):
    if not failures:
        return ""
    lines = ["[抓取异常]"]
    for _sid, name, err, n in failures:
        lines.append("  %s -- %s（已连续失败 %d 次）" % (name, err, n))
    return "\n".join(lines) + "\n\n"


def send_email(smtp_cfg, subject, text_body, html_body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["user"]
    msg["To"] = smtp_cfg["to"]
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    # 原来一次失败就放弃。DNS 或网络没就绪时那一封就彻底丢了，
    # 得等到第二天才补发。
    last = None
    for attempt in range(4):
        try:
            with smtplib.SMTP_SSL(smtp_cfg["host"], int(smtp_cfg["port"]), timeout=60) as srv:
                srv.login(smtp_cfg["user"], smtp_cfg["app_password"])
                srv.send_message(msg)
            log.info("邮件已发送：%s", subject)
            return
        except smtplib.SMTPAuthenticationError:
            raise                       # 密码错了，重试多少次都一样
        except Exception as exc:
            last = exc
            if attempt < 3:
                wait = 2 ** attempt * 5
                log.warning("发信失败（第 %d 次）：%s；%d 秒后重试", attempt + 1, exc, wait)
                time.sleep(wait)
    raise last


def today_cn():
    d = date.today()
    return "%d月%d日" % (d.month, d.day)


def build_verification_email(items_by_source, counts, failures, cfg):
    """配置验证信 —— 邮箱配好后一次性发出，附当前完整快照作为日后增量的参照基准。"""
    total = sum(counts.values())
    subject = "[RA岗位] 配置成功 · 当前共 %d 个在招职位" % total

    intro_html = (
        '<div style="background:#ffffff;border:1px solid #e2e5ea;border-radius:6px;'
        'padding:16px;margin-bottom:14px;">'
        '<div style="font-size:17px;font-weight:700;color:#1f7a3d;">邮件推送已配置成功</div>'
        '<div style="color:#4b5563;font-size:13px;margin-top:8px;line-height:1.6;">'
        '此后每天 09:07 自动检查下列三个来源，只把<strong>新增岗位</strong>推给你；'
        '当天没有新增也会发一封简短的状态信，所以<strong>收不到邮件就说明出了问题</strong>。<br>'
        '下面是建立基线时的当前快照，之后收到的都是相对它的增量。</div></div>'
    )
    intro_text = (
        "邮件推送已配置成功。\n\n"
        "此后每天 09:07 自动检查三个来源，只推送新增岗位；当天没有新增也会发一封状态信，"
        "所以收不到邮件就说明出了问题。\n"
        "下面是建立基线时的当前快照，之后收到的都是相对它的增量。\n\n"
    )

    failed_sids = set(f[0] for f in failures)
    parts = [failure_banner_html(failures), intro_html, counts_block(counts, failed_sids)]
    tparts = [failure_banner_text(failures), intro_text, counts_text(counts, failed_sids), "\n"]

    if cfg.get("verification_email_full_list", True):
        for src in SOURCES:
            items = items_by_source.get(src["id"]) or []
            if not items:
                continue
            parts.append(h_section("%s · %d 个在招" % (src["name"], len(items))))
            tparts.append("\n== %s · %d 个在招 ==\n" % (src["name"], len(items)))
            rows = []
            for it in items:
                inst = it.get("institution") or ""
                rows.append(
                    '<li style="margin:0 0 6px 0;line-height:1.5;">'
                    '<a href="%s" style="color:#1a4fa0;text-decoration:none;">%s</a>'
                    '<span style="color:#6b7280;font-size:12px;"> — %s</span></li>'
                    % (esc(it.get("link", "")), esc(it.get("title", "")), esc(inst))
                )
                tparts.append("  - %s — %s\n    %s\n" % (it.get("title", ""), inst, it.get("link", "")))
            parts.append(
                '<ul style="margin:0;padding-left:20px;font-size:14px;">%s</ul>' % "".join(rows)
            )

    parts.append(
        '<div style="color:#9099a8;font-size:12px;margin-top:20px;line-height:1.6;">'
        '配置文件：%s　·　日志：%s<br>'
        '想改成「只在有新增时才发信」，把 config.json 里的 daily_email_even_if_empty 设为 false。</div>'
        % (esc(str(CONFIG_PATH)), esc(str(LOG_DIR / "watch.log")))
    )
    tparts.append(
        "\n配置文件：%s\n日志：%s\n"
        "想改成「只在有新增时才发信」，把 config.json 里的 daily_email_even_if_empty 设为 false。\n"
        % (CONFIG_PATH, LOG_DIR / "watch.log")
    )
    return subject, "".join(tparts), page_shell("".join(parts))


def build_daily_email(new_by_source, counts, failures, last_new_at):
    """每日更新信 —— 主体是当日新增岗位；无新增则是一封简短状态信。"""
    total_new = sum(len(v) for v in new_by_source.values())
    total = sum(counts.values())
    warn = "%d 个来源抓取失败 · " % len(failures) if failures else ""

    if total_new:
        subject = "[RA岗位] %d 个新岗位 · %s%s" % (total_new, warn, today_cn())
    else:
        subject = "[RA岗位] 今日无更新 · %s共 %d 个在招 · %s" % (warn, total, today_cn())

    parts = [failure_banner_html(failures)]
    tparts = [failure_banner_text(failures)]

    if total_new:
        parts.append(
            '<div style="font-size:17px;font-weight:700;color:#111827;margin:0 0 14px 0;">'
            '今日新增 %d 个岗位</div>' % total_new
        )
        tparts.append("今日新增 %d 个岗位\n\n" % total_new)
        for src in SOURCES:
            items = new_by_source.get(src["id"]) or []
            if not items:
                continue
            parts.append(h_section("%s · %d 个新增" % (src["name"], len(items))))
            tparts.append("\n== %s · %d 个新增 ==\n\n" % (src["name"], len(items)))
            for it in items:
                parts.append(card_html(it, src["name"]))
                tparts.append(card_text(it, src["name"]) + "\n\n")
        parts.append(h_section("当前在招总数"))
        tparts.append("\n[当前在招总数]\n")
    else:
        # 有来源抓取失败时，不能说「都已正常检查」—— 那与顶部的告警自相矛盾。
        if failures:
            checked = "已成功检查的来源里没有发现新职位；下列来源本次没查成（见上方告警）：%s。" % (
                "、".join(f[1] for f in failures)
            )
        else:
            checked = "三个来源都已正常检查，没有发现新职位。"
        hint = ""
        if last_new_at:
            hint = "<br>上次出现新增是 %s。" % esc(last_new_at)
        parts.append(
            '<div style="background:#ffffff;border:1px solid #e2e5ea;border-radius:6px;'
            'padding:16px;margin-bottom:14px;">'
            '<div style="font-size:17px;font-weight:700;color:#111827;">今日无新增岗位</div>'
            '<div style="color:#4b5563;font-size:13px;margin-top:8px;line-height:1.6;">'
            '%s%s</div></div>' % (esc(checked), hint)
        )
        tparts.append("今日无新增岗位。%s\n" % checked)
        if last_new_at:
            tparts.append("上次出现新增是 %s。\n" % last_new_at)
        tparts.append("\n[当前在招总数]\n")

    failed_sids = set(f[0] for f in failures)
    parts.append(counts_block(counts, failed_sids))
    tparts.append(counts_text(counts, failed_sids) + "\n")
    return subject, "".join(tparts), page_shell("".join(parts))


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def setup_logging(verbose=False):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    # utf-8-sig：带 BOM，这样 PowerShell 的 Get-Content 和记事本才不会把中文按 GBK 读成乱码。
    # 追加模式下 BOM 只在文件新建时写一次，不会重复。
    fh = RotatingFileHandler(
        LOG_DIR / "watch.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8-sig"
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # pythonw.exe 下 sys.stdout 为 None（计划任务就是这么跑的），此时只写文件日志。
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def print_dry_run(results):
    for src in SOURCES:
        res = results.get(src["id"])
        if res is None:
            continue
        if not res["ok"]:
            print("\n=== %s === 失败：%s" % (src["name"], res["error"]))
            continue
        items = res["items"]
        print("\n=== %s === 解析到 %d 条" % (src["name"], len(items)))
        rel = [i for i in items if "/sites/" in i.get("link", "")]
        if rel:
            print("    （其中 %d 条相对路径已拼成绝对地址，例：%s）" % (len(rel), rel[0]["link"]))
        for it in items[:2]:
            print("  - 标题: %s" % it.get("title"))
            for key, label in FIELD_LABELS_CN:
                if it.get(key):
                    print("    %s: %s" % (label, it[key]))
            if it.get("notes"):
                print("    其他: %s" % it["notes"])
            if it.get("tags"):
                print("    标签: %s" % " · ".join(it["tags"]))
            print("    链接: %s" % it.get("link"))
    total = sum(len(r["items"]) for r in results.values() if r["ok"])
    print("\n合计 %d 条。" % total)


def main(argv=None):
    ap = argparse.ArgumentParser(description="pre-doc / RA 招聘公告监控与邮件推送")
    ap.add_argument("--dry-run", action="store_true", help="只抓取解析并打印，不发信、不写 state")
    ap.add_argument("--source", action="append", metavar="ID",
                    help="只跑指定来源（可重复）：predoc / nber / nber_external")
    ap.add_argument("--test-email", action="store_true", help="只发一封测试邮件，验证 SMTP 配置")
    ap.add_argument("--resend-verification", action="store_true",
                    help="清除已验证标记，下次运行重发配置验证信")
    ap.add_argument("--rebaseline", action="store_true", help="用当前页面重置 state（不发信）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    cfg = load_config()
    smtp_cfg = cfg["smtp"]
    state = load_state()

    if args.resend_verification:
        state["meta"]["email_verified"] = False
        state["meta"]["smtp_fingerprint"] = ""
        save_state(state)
        log.info("已清除验证标记，下次运行将重发配置验证信")
        return 0

    if args.test_email:
        if not smtp_cfg.get("app_password"):
            log.error("config.json 里的 app_password 还是空的，无法发信")
            return 2
        body = "这是一封测试邮件，说明 SMTP 配置可用。\n发件：%s\n收件：%s\n" % (
            smtp_cfg["user"], smtp_cfg["to"])
        send_email(smtp_cfg, "[RA岗位] SMTP 测试邮件", body,
                   page_shell('<div style="font-size:15px;">%s</div>'
                              % esc(body).replace("\n", "<br>")))
        return 0

    # ---- 抓取 ----
    wanted = args.source or [s["id"] for s in SOURCES if cfg["sources"].get(s["id"], True)]
    unknown = [w for w in wanted if w not in SOURCE_BY_ID]
    if unknown:
        log.error("未知来源：%s", ", ".join(unknown))
        return 2

    # 唤醒后补跑时网络往往还没起来，先等一等再开工
    probe = [urlparse(SOURCE_BY_ID[sid]["url"]).hostname for sid in wanted]
    probe.append(smtp_cfg["host"])
    wait_for_network([h for h in probe if h])

    log.info("开始检查 %d 个来源", len(wanted))
    results = {}
    for sid in wanted:
        results[sid] = collect(SOURCE_BY_ID[sid])

    if args.dry_run:
        print_dry_run(results)
        return 0

    # 失败计数：成功清零，失败累加；连续失败会在邮件顶部亮红。
    failures = []
    for sid, res in results.items():
        name = SOURCE_BY_ID[sid]["name"]
        if res["ok"]:
            state["meta"]["failures"][sid] = 0
        else:
            n = state["meta"]["failures"].get(sid, 0) + 1
            state["meta"]["failures"][sid] = n
            failures.append((sid, name, res["error"], n))

    # ---- 比对 ----
    first_run = not state["meta"].get("initialized")
    snapshots = {}
    new_by_source = {}
    for sid, res in results.items():
        if not res["ok"]:
            continue  # 失败来源保持原 state 不动，绝不清空
        known = state["sources"].get(sid, {})
        snap = {}
        fresh = []
        for it in res["items"]:
            key = item_key(sid, it)
            if key in snap:
                continue  # 同一页面内的完全重复条目
            snap[key] = it
            if not first_run and key not in known:
                fresh.append(it)
        snapshots[sid] = snap
        if fresh:
            new_by_source[sid] = fresh

    # 报数用去重之后的快照，跟真正入库跟踪的条数一致（站方页面上偶有完全重复的条目）。
    counts = dict((sid, len(snap)) for sid, snap in snapshots.items())
    items_by_source = dict((sid, list(snap.values())) for sid, snap in snapshots.items())

    total_new = sum(len(v) for v in new_by_source.values())
    if first_run:
        log.info("首次运行：建立基线 %d 条", sum(len(s) for s in snapshots.values()))
    else:
        log.info("发现新增 %d 条", total_new)

    if args.rebaseline:
        for sid, snap in snapshots.items():
            state["sources"][sid] = snap
        state["meta"]["initialized"] = True
        state["meta"]["last_run_at"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
        log.info("已用当前页面重置 state（未发信）")
        return 0

    # ---- 发信 ----
    fingerprint = smtp_fingerprint(smtp_cfg)
    if state["meta"].get("smtp_fingerprint") not in ("", fingerprint):
        log.info("检测到收发邮箱变更，将重新发送配置验证信")
        state["meta"]["email_verified"] = False

    commit = True
    if not smtp_cfg.get("app_password"):
        # 邮箱还没配好：照常建库，但不发信、也不打验证标记。
        # 于是用户填好密码后的第一次运行会自动触发配置验证信。
        log.warning("邮箱未配置（app_password 为空），跳过发信；填好密码后下次运行会自动发送配置验证信")
    else:
        try:
            if not state["meta"].get("email_verified"):
                subject, text, html_body = build_verification_email(items_by_source, counts, failures, cfg)
                send_email(smtp_cfg, subject, text, html_body)
                state["meta"]["email_verified"] = True
                state["meta"]["smtp_fingerprint"] = fingerprint
            elif total_new or cfg.get("daily_email_even_if_empty", True):
                subject, text, html_body = build_daily_email(
                    new_by_source, counts, failures, state["meta"].get("last_new_at")
                )
                send_email(smtp_cfg, subject, text, html_body)
            else:
                log.info("无新增且已关闭空邮件，本次不发信")
        except Exception as exc:
            # 发信失败就不提交本次新增，下次运行会重新报告，不会漏。
            # 带上堆栈：否则代码里的 bug 会被伪装成"网络故障"，很难查。
            commit = False
            log.error("发送邮件失败：%s（本次新增不写入 state，下次运行会重试）", exc, exc_info=True)

    if commit:
        for sid, snap in snapshots.items():
            state["sources"][sid] = snap
        state["meta"]["initialized"] = True
        if total_new:
            state["meta"]["last_new_at"] = date.today().isoformat()
    state["meta"]["last_run_at"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    log.info("本次运行结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
