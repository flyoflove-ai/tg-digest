"""
tg-digest 출력 포맷터 (drop-in).

digest.py 에서:
    from formatter import format_rules, format_gemini, send_report
    ...
    summary = format_gemini(summarize_gemini(messages))   # Gemini 경로
    summary = format_rules(messages, SECTOR_KEYWORDS)      # 규칙 기반 경로
    send_report(TELEGRAM_BOT_TOKEN, TARGET_CHAT_ID, summary, n_msgs=len(messages))

- 링크/이미지 안내 전부 제거
- 텔레그램 HTML parse_mode 사용 (굵게/구분선), 실패 시 plain text 자동 재전송
- 이모지가 '??' 로 깨지는 문제: JSON 전송 시 ensure_ascii 이스케이프로 원천 차단
- 3900자 분할 시 HTML 태그가 잘리지 않도록 섹션 경계에서만 분할
"""
import html
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
WS_RE = re.compile(r"[ \t\u00a0]+")
MULTI_NL_RE = re.compile(r"\n{2,}")

SECTOR_ICON = [  # (부분일치 키워드, 아이콘) — SECTOR_KEYWORDS 의 키 이름과 느슨하게 매칭
    ("반도체", "🔬"), ("메모리", "🔬"),
    ("전력", "⚡"), ("그리드", "⚡"),
    ("방산", "🛡️"),
    ("2차전지", "🔋"), ("배터리", "🔋"), ("소재", "🔋"),
    ("로봇", "🤖"),
    ("신재생", "🌱"), ("원전", "🌱"),
    ("매크로", "🌐"),
    ("바이오", "🧬"), ("조선", "🚢"), ("자동차", "🚗"), ("AI", "🧠"),
    ("기타", "📎"),
]
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"

TITLE_LEN = 46      # 항목 제목 최대 길이
BODY_LEN = 170      # 항목 본문 최대 길이
MAX_PER_SECTOR = 8  # 섹터당 최대 항목 수
TG_LIMIT = 3900


# ------------------------------------------------------------ helpers
def _icon(sector: str) -> str:
    for k, ic in SECTOR_ICON:
        if k.lower() in sector.lower():
            return ic
    return "📌"


def _clean(text: str) -> str:
    """URL 제거, 공백 정리."""
    t = URL_RE.sub("", text or "")
    t = t.replace("\r", "")
    t = "\n".join(WS_RE.sub(" ", ln).strip() for ln in t.split("\n"))
    t = MULTI_NL_RE.sub("\n", t).strip(" \n-–—·•▪|")
    return t


def _cut(s: str, n: int) -> str:
    """n자 안에서 문장/어절 경계로 자르고 … 부착."""
    s = s.strip()
    if len(s) <= n:
        return s
    head = s[:n]
    for sep in ("다. ", "요. ", ". ", "! ", "? ", ") ", "; "):
        i = head.rfind(sep)
        if i >= n * 0.5:
            return head[: i + len(sep) - 1].rstrip() + " …"
    i = head.rfind(" ")
    if i >= n * 0.6:
        head = head[:i]
    return head.rstrip() + " …"


def _split_title_body(text: str):
    """첫 줄(또는 첫 문장)을 제목으로, 나머지를 본문으로. 제목이 잘리면 잘린 부분은 본문으로 넘김."""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return "", ""
    first, rest_lines = lines[0], lines[1:]
    first = re.sub(r"^[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50\u2705]+\s*", "", first)  # 선두 이모지 제거
    if len(first) <= TITLE_LEN:
        title, rest = first, ""
    else:
        m = re.match(r"(.{8,%d}?[.다요!?\"”])(\s|$)" % TITLE_LEN, first)
        if m:
            title, rest = m.group(1), first[m.end():]
        else:
            cut = first.rfind(" ", 0, TITLE_LEN)
            cut = cut if cut >= TITLE_LEN * 0.5 else TITLE_LEN
            title, rest = first[:cut].rstrip() + " …", first[cut:]
    body = "\n".join(([rest.strip()] if rest.strip() else []) + rest_lines)
    return title, body.strip()


def _body_lines(body: str, n_max=4, width=95):
    """본문에 불릿(•/▪/-)이 2개 이상이면 줄 단위로 분리, 아니면 한 덩어리."""
    parts = [p.strip(" •▪·-–") for p in re.split(r"\s*(?:^|\n|\s)[•▪·]\s*|\n\s*[-–]\s+", body) if p.strip(" •▪·-–")]
    if len(parts) >= 2:
        out = ["· " + _cut(p, width) for p in parts[:n_max]]
        if len(parts) > n_max:
            out.append(f"· … 외 {len(parts) - n_max}개")
        return out
    return [_cut(body, BODY_LEN)]


def _norm_key(s: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", s)[:28].lower()


def _fmt_time(t: str) -> str:
    """'08/28 10:52' 또는 '10:52' → '10:52'"""
    m = re.search(r"(\d{1,2}:\d{2})", t or "")
    return m.group(1) if m else (t or "")


# ------------------------------------------------------------ rule-based
def format_rules(messages, sector_keywords) -> str:
    """
    LLM 없이 폴백. messages: [{'time','sender','text','image'}], sector_keywords: {섹터: [키워드...]}
    반환: 텔레그램 HTML 문자열 (헤더 제외 본문만).
    """
    by_sector = {}
    seen, dropped_link_only, dropped_img_only = set(), 0, 0

    for m in messages:
        raw = m.get("text") or ""
        body = _clean(raw)
        if not body:
            if URL_RE.search(raw):
                dropped_link_only += 1
            elif m.get("image"):
                dropped_img_only += 1
            continue
        if len(body) < 8:
            continue
        key = _norm_key(body)
        if key in seen:
            continue
        seen.add(key)

        low = raw.lower()
        sector = next((s for s, kws in sector_keywords.items()
                       if any(k.lower() in low for k in kws)), "기타")
        by_sector.setdefault(sector, []).append({"time": _fmt_time(m.get("time")), "text": body})

    # 섹터 순서: sector_keywords 순서 → 기타 마지막
    order = [s for s in sector_keywords if s in by_sector] + \
            [s for s in by_sector if s not in sector_keywords]

    total = sum(len(v) for v in by_sector.values())
    out = [f"💬 정리 {total}건 · 섹터 {len(order)}개"]

    for sector in order:
        items = by_sector[sector]
        block = [f"\n<b>{_icon(sector)} {html.escape(sector)}</b>  <i>{len(items)}건</i>"]
        for i, it in enumerate(items[:MAX_PER_SECTOR]):
            title, body = _split_title_body(it["text"])
            num = CIRCLED[i] if i < len(CIRCLED) else f"{i+1}."
            line = f"{num} <b>{html.escape(title)}</b>"
            if it["time"]:
                line += f"  <i>{it['time']}</i>"
            block.append(line)
            if body:
                for bl in _body_lines(body):
                    block.append("    " + html.escape(bl))
        if len(items) > MAX_PER_SECTOR:
            block.append(f"    <i>… 외 {len(items) - MAX_PER_SECTOR}건</i>")
        out.append("\n".join(block))

    return "\n".join(out)


# ------------------------------------------------------------ gemini post-process
_HDR_RE = re.compile(r"^\s*(\d)\)\s*(.+?)\s*$")
_LINK_HDR_RE = re.compile(r"^\s*\d\)\s*.*링크")


def format_gemini(text: str) -> str:
    """
    Gemini plain-text 출력 → HTML 정돈.
    - 'N) 제목' 줄을 굵게 + 위 빈줄
    - '주요 링크' 섹션 통째로 제거, 본문 내 URL 제거
    - 섹터 소제목(짧고 불릿 없는 줄)은 굵게
    """
    lines, skip = [], False
    for raw in (text or "").split("\n"):
        ln = raw.rstrip()
        if _LINK_HDR_RE.match(ln):
            skip = True
            continue
        hm = _HDR_RE.match(ln)
        if hm:
            skip = False
            lines.append("")
            lines.append(f"<b>▍{html.escape(hm.group(2))}</b>")
            continue
        if skip:
            continue
        ln = URL_RE.sub("", ln).rstrip()
        if not ln.strip():
            if lines and lines[-1] != "":
                lines.append("")
            continue
        s = ln.strip()
        is_bullet = s[0] in "-•▪·*◦▫▸►" or re.match(r"^\d+[.)]", s)
        if not is_bullet and len(s) <= 24 and (s.endswith(":") or s.endswith("]") or s.startswith(("[", "【", "▣", "■", "◆"))):
            lines.append(f"<b>{html.escape(s.strip('[]【】:▣■◆ '))}</b>")
            continue
        if is_bullet:
            s = "▪ " + s.lstrip("-•▪·*◦▫▸► ")
            lines.append(html.escape(s))
        else:
            lines.append(html.escape(ln))
    out = "\n".join(lines).strip()
    return MULTI_NL_RE.sub("\n\n", out)


# ------------------------------------------------------------ send
def _header(n_msgs, mode):
    now = datetime.now(KST)
    wd = "월화수목금토일"[now.weekday()]
    tag = " · 규칙모드" if mode == "rules" else ""
    return (f"<b>📰 텔레방 요약</b>  {now:%m/%d}({wd}) {now:%H:%M}{tag}\n"
            f"📥 수집 {n_msgs}건\n"
            "━━━━━━━━━━━━━━━━")


def _chunks(text: str, limit=TG_LIMIT):
    """빈 줄(섹션) 경계에서만 분할해 HTML 태그가 잘리지 않게."""
    parts, cur = [], ""
    for para in text.split("\n\n"):
        cand = para if not cur else cur + "\n\n" + para
        if len(cand) <= limit:
            cur = cand
            continue
        if cur:
            parts.append(cur)
        cur = para
        while len(cur) > limit:          # 한 문단이 너무 길면 줄 단위로
            cut = cur.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(cur[:cut])
            cur = cur[cut:].lstrip("\n")
    if cur:
        parts.append(cur)
    return parts


_TAG_RE = re.compile(r"</?(b|i|code|u|s)>")


def _post(token, payload):
    data = json.dumps(payload).encode("utf-8")   # ensure_ascii=True → 이모지 \uXXXX 이스케이프
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def send_report(token, chat_id, body, n_msgs=0, mode="gemini"):
    full = _header(n_msgs, mode) + "\n\n" + body
    parts = _chunks(full)
    n = len(parts)
    for i, p in enumerate(parts):
        if n > 1:
            p += f"\n\n<i>({i+1}/{n})</i>"
        payload = {"chat_id": chat_id, "text": p,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            _post(token, payload)
        except Exception as e:                       # HTML 파싱 실패 등 → plain 재전송
            print(f"HTML send failed ({e}); resend as plain text")
            plain = html.unescape(_TAG_RE.sub("", p))
            _post(token, {"chat_id": chat_id, "text": plain,
                          "disable_web_page_preview": True})
        time.sleep(1)
