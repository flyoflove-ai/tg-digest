# -*- coding: utf-8 -*-
"""
텔레그램 그룹 메시지 수집 → 요약 → 발송 (하루 4회, 무료 스택) — v4

v2: 이미지 요약(Gemini 멀티모달), 상세도 강화, 모델 폴백
v3: digests/YYYY-MM-DD.md 저장 (sentiment_bot 연동), ListModels 자동 발견
v4 변경점 (출력 포맷 전면 개편):
  - 텔레그램 HTML parse_mode 사용: 섹터 소제목/항목 제목 굵게, 시각은 기울임
  - 규칙 기반 폴백: 제목/본문 분리, 메시지 내 불릿(•) 줄 단위 전개, 중복 제거
  - 링크 섹션·"이미지 N건 포함" 안내·본문 내 URL 전부 제거 (링크만 있는 메시지는 항목에서 제외)
  - Gemini 출력 후처리: 'N) 제목' 굵게, 링크 섹션 자동 삭제
  - 이모지가 '??' 로 깨지던 문제 수정 (원본 소스 파일의 이모지가 '?'로 손상되어 있었음)
  - 3900자 분할을 섹션 경계에서만 수행 + (1/2) 페이지 표기, HTML 오류 시 plain text 자동 재전송
  - digests/ 저장본은 HTML 태그를 뺀 plain text (sentiment_bot 호환)

필요 Secrets: TELEGRAM_BOT_TOKEN / SOURCE_CHAT_ID / TARGET_CHAT_ID / GEMINI_API_KEY(선택)
"""

import base64
import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
TARGET_CHAT_ID = os.environ["TARGET_CHAT_ID"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

GEMINI_MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "")] if m] + [
    "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash",
]

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
STATE_FILE = "state.json"
KST = timezone(timedelta(hours=9))

MAX_IMAGES = 8               # 회당 Gemini에 넣을 이미지 최대 개수
MAX_IMAGE_BYTES = 4_000_000  # 이미지 1장 최대 크기 (약 4MB)

SECTOR_KEYWORDS = {
    "반도체/메모리": ["반도체", "하이닉스", "삼성전자", "HBM", "DRAM", "낸드", "NAND",
                  "파운드리", "TSMC", "엔비디아", "Nvidia", "CoWoS", "웨이퍼", "소부장",
                  "마이크론", "Micron", "CAPEX", "캐펙스", "MLCC"],
    "전력기기/그리드": ["전력", "변압기", "HD현대일렉트릭", "효성중공업", "LS일렉트릭",
                   "그리드", "송전", "ESS"],
    "방산": ["방산", "한화에어로", "LIG넥스원", "현대로템", "KAI", "수출계약", "폴란드"],
    "2차전지/소재": ["2차전지", "배터리", "양극재", "음극재", "리튬", "LG에너지",
                 "에코프로", "포스코", "POSCO", "전해질", "FEOC"],
    "로봇": ["로봇", "보스턴다이내믹스", "레인보우로보틱스", "휴머노이드", "두산로보틱스"],
    "신재생": ["태양광", "풍력", "신재생", "수소", "원전", "SMR"],
    "매크로": ["금리", "연준", "Fed", "CPI", "환율", "달러", "국채", "고용", "FOMC",
             "관세", "유가", "WTI"],
}

SECTOR_ICON = {
    "반도체/메모리": "🔬", "전력기기/그리드": "⚡", "방산": "🛡️", "2차전지/소재": "🔋",
    "로봇": "🤖", "신재생": "🌱", "매크로": "🌐", "기타": "📎",
}
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"

TITLE_LEN = 46       # 항목 제목 최대 길이
BODY_LEN = 170       # 항목 본문(한 덩어리일 때) 최대 길이
SUBLINE_LEN = 95     # 불릿 전개 시 줄당 최대 길이
MAX_PER_SECTOR = 8   # 섹터당 최대 항목 수
TG_LIMIT = 3900      # 텔레그램 메시지 분할 기준

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
WS_RE = re.compile(r"[ \t\u00a0]+")
MULTI_NL_RE = re.compile(r"\n{2,}")
LEAD_EMOJI_RE = re.compile(r"^[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50\u2705\uFE0F]+\s*")
TAG_RE = re.compile(r"</?(b|i|code|u|s)>")


# ---------------------------------------------------------------- utils
def http_json(url, payload=None, headers=None, retries=3):
    # ensure_ascii=True(기본) → 이모지가 \uXXXX 로 이스케이프되어 인코딩 경로와 무관하게 안전
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
    )
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def http_bytes(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"offset": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- collect
def download_photo(file_id):
    """텔레그램 사진 다운로드 → (base64, mime). 실패/과대용량 시 None."""
    try:
        info = http_json(f"{API}/getFile", {"file_id": file_id})
        path = info["result"]["file_path"]
        raw = http_bytes(f"{FILE_API}/{path}")
        if len(raw) > MAX_IMAGE_BYTES:
            return None
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        return base64.b64encode(raw).decode(), mime
    except Exception as e:
        print(f"photo download failed: {e}", file=sys.stderr)
        return None


def collect_messages(state):
    """getUpdates 증분 수집. 텍스트 + 사진(캡션 포함) 모두 수집."""
    messages, offset = [], state.get("offset", 0)
    image_count = 0
    while True:
        res = http_json(
            f"{API}/getUpdates",
            {"offset": offset + 1, "timeout": 0, "limit": 100,
             "allowed_updates": ["message", "channel_post"]},
        )
        updates = res.get("result", [])
        if not updates:
            break
        for u in updates:
            offset = max(offset, u["update_id"])
            msg = u.get("message") or u.get("channel_post")
            if not msg or msg.get("chat", {}).get("id") != SOURCE_CHAT_ID:
                continue

            text = (msg.get("text") or msg.get("caption") or "").strip()
            photo = msg.get("photo")  # 사이즈별 리스트, 마지막이 최대 해상도
            if not text and not photo:
                continue

            sender = (msg.get("from", {}).get("first_name")
                      or msg.get("author_signature") or "")
            ts = datetime.fromtimestamp(msg["date"], tz=KST)
            entry = {"time": ts.strftime("%m/%d %H:%M"),
                     "sender": sender, "text": text, "image": None}

            if photo and image_count < MAX_IMAGES:
                dl = download_photo(photo[-1]["file_id"])
                if dl:
                    entry["image"] = dl  # (b64, mime)
                    image_count += 1
            # 한도 초과 이미지는 안내문 없이 텍스트만 사용 (v4)

            messages.append(entry)
        if len(updates) < 100:
            break
    state["offset"] = offset
    print(f"images attached: {image_count}")
    return messages


# ---------------------------------------------------------------- gemini
PROMPT_HEADER = """당신은 한국 주식시장을 담당하는 시니어 애널리스트입니다.
아래는 투자 정보 텔레그램 방에 최근 올라온 메시지들입니다(텍스트 + 이미지).
이미지는 대부분 차트, 리포트 캡처, 뉴스 스크린샷, 표입니다. 이미지 안의 수치·종목명·
목표주가·표 내용까지 읽어서 요약에 반영하세요.

다음 형식으로 정리하세요:

1) 오늘의 핵심 (5~7줄)
   - 가장 중요한 내용을 우선순위대로. 각 줄에 근거 수치 포함

2) 섹터별 상세 정리 — 해당 내용이 있는 섹터만
   (반도체/메모리, 전력기기·그리드, 방산, 2차전지/소재, 로봇, 신재생, 매크로, 기타)
   - 섹터명은 대괄호로 한 줄에 단독 표기: [반도체/메모리]
   - 섹터당 주요 항목 각각 2~4줄로 상세히
   - 수치, 목표주가, 증권사명, 날짜는 절대 생략하지 말 것
   - 팩트(발표/공시/수주)와 의견(전망/추정)을 구분해 표기

3) 언급 종목 리스트
   - 종목명: 언급 맥락 + 방향성(긍정/부정/중립) 한 줄씩

규칙:
- URL/링크는 출력하지 말 것
- 중복 내용은 합치되, 정보 손실 없이 통합
- 광고/잡담/인사만 제외하고 나머지는 최대한 보존
- 요약이 너무 짧아지지 않게: 원문 정보량의 골격이 유지되어야 함
- 텔레그램 발송용: 마크다운 특수문자(*, #, `) 없이 plain text + 불릿(-)만 사용
"""


def build_gemini_parts(messages):
    parts = [{"text": PROMPT_HEADER + "\n--- 메시지 시작 ---\n"}]
    total_chars = 0
    for m in messages:
        line = f"\n[{m['time']}] {m['sender']}: {m['text'][:2000]}"
        total_chars += len(line)
        if total_chars > 150_000:
            parts.append({"text": "\n(이후 메시지 생략 - 분량 초과)"})
            break
        parts.append({"text": line})
        if m["image"]:
            b64, mime = m["image"]
            parts.append({"text": " ↓ 첨부 이미지:"})
            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
    parts.append({"text": "\n--- 메시지 끝 ---"})
    return parts


def discover_gemini_models():
    """ListModels API로 사용 가능한 flash 모델 조회 — 모델 지원 종료 대응."""
    try:
        res = http_json("https://generativelanguage.googleapis.com/v1beta/models"
                        f"?key={GEMINI_API_KEY}&pageSize=200")
        names = [m["name"].split("/")[-1] for m in res.get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        flash = [n for n in names if "flash" in n
                 and all(x not in n for x in ("lite", "image", "tts", "audio", "live", "exp"))]
        return sorted(flash, reverse=True)[:3]
    except Exception as e:
        print(f"model discovery failed: {e}", file=sys.stderr)
        return []


def summarize_gemini(messages):
    parts = build_gemini_parts(messages)
    last_err = None
    models = []
    for m in discover_gemini_models() + GEMINI_MODELS:
        if m not in models:
            models.append(m)
    for model in models:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={GEMINI_API_KEY}")
        try:
            res = http_json(url, {
                "contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0.3,
                                     "maxOutputTokens": 8192},
            }, retries=1)
            print(f"gemini model used: {model}")
            return res["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            last_err = e
            continue
    raise last_err


# ---------------------------------------------------------------- format helpers
def _clean(text):
    """URL 제거, 공백 정리."""
    t = URL_RE.sub("", text or "").replace("\r", "")
    t = "\n".join(WS_RE.sub(" ", ln).strip() for ln in t.split("\n"))
    return MULTI_NL_RE.sub("\n", t).strip(" \n-–—·•▪|")


def _cut(s, n):
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


def _split_title_body(text):
    """첫 줄(또는 첫 문장)을 제목으로, 나머지를 본문으로. 제목이 잘리면 잘린 부분은 본문으로."""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return "", ""
    first, rest_lines = LEAD_EMOJI_RE.sub("", lines[0]), lines[1:]
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


def _body_lines(body, n_max=4):
    """본문에 불릿(•/▪/-)이 2개 이상이면 줄 단위로 전개, 아니면 한 덩어리."""
    parts = [p.strip(" •▪·-–") for p in
             re.split(r"\s*(?:^|\n|\s)[•▪·]\s*|\n\s*[-–]\s+", body) if p.strip(" •▪·-–")]
    if len(parts) >= 2:
        out = ["· " + _cut(p, SUBLINE_LEN) for p in parts[:n_max]]
        if len(parts) > n_max:
            out.append(f"· … 외 {len(parts) - n_max}개")
        return out
    return [_cut(body, BODY_LEN)]


def _norm_key(s):
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", s)[:28].lower()


def _hhmm(t):
    m = re.search(r"(\d{1,2}:\d{2})", t or "")
    return m.group(1) if m else (t or "")


# ---------------------------------------------------------------- rule-based (HTML)
def summarize_rules(messages):
    """LLM 없이 폴백: 키워드 섹터 분류 → 제목/본문 정돈. 링크·이미지 안내 없음."""
    by_sector, seen = {}, set()
    for m in messages:
        raw = m["text"] or ""
        body = _clean(raw)
        if len(body) < 8:          # 링크만 / 이미지만 / 잡담 한두 글자
            continue
        key = _norm_key(body)
        if key in seen:
            continue
        seen.add(key)
        low = raw.lower()
        sector = next((s for s, kws in SECTOR_KEYWORDS.items()
                       if any(k.lower() in low for k in kws)), "기타")
        by_sector.setdefault(sector, []).append({"time": _hhmm(m["time"]), "text": body})

    order = [s for s in SECTOR_KEYWORDS if s in by_sector] + (["기타"] if "기타" in by_sector else [])
    total = sum(len(v) for v in by_sector.values())
    out = [f"💬 정리 {total}건 · 섹터 {len(order)}개"]

    for sector in order:
        items = by_sector[sector]
        block = [f"\n<b>{SECTOR_ICON.get(sector, '📌')} {html.escape(sector)}</b>  <i>{len(items)}건</i>"]
        for i, it in enumerate(items[:MAX_PER_SECTOR]):
            title, body = _split_title_body(it["text"])
            num = CIRCLED[i] if i < len(CIRCLED) else f"{i + 1}."
            line = f"{num} <b>{html.escape(title)}</b>"
            if it["time"]:
                line += f"  <i>{it['time']}</i>"
            block.append(line)
            for bl in _body_lines(body) if body else []:
                block.append("    " + html.escape(bl))
        if len(items) > MAX_PER_SECTOR:
            block.append(f"    <i>… 외 {len(items) - MAX_PER_SECTOR}건</i>")
        out.append("\n".join(block))
    return "\n".join(out)


# ---------------------------------------------------------------- gemini post-process (HTML)
_HDR_RE = re.compile(r"^\s*(\d)\)\s*(.+?)\s*$")
_LINK_HDR_RE = re.compile(r"^\s*\d\)\s*.*링크")


def format_gemini(text):
    """Gemini plain-text → HTML 정돈: 'N) 제목' 굵게, [섹터] 굵게, 링크 섹션/URL 제거, 불릿 통일."""
    lines, skip = [], False
    for raw in (text or "").split("\n"):
        ln = raw.rstrip()
        if _LINK_HDR_RE.match(ln):
            skip = True
            continue
        hm = _HDR_RE.match(ln)
        if hm:
            skip = False
            lines += ["", f"<b>▍{html.escape(hm.group(2))}</b>"]
            continue
        if skip:
            continue
        ln = URL_RE.sub("", ln).rstrip()
        s = ln.strip()
        if not s:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        is_bullet = s[0] in "-•▪·*◦▫▸►" or re.match(r"^\d+[.)]\s", s)
        if not is_bullet and len(s) <= 24 and (
                s.endswith(":") or s.endswith("]") or s.startswith(("[", "【", "▣", "■", "◆"))):
            name = s.strip("[]【】:▣■◆ ")
            lines.append(f"<b>{SECTOR_ICON.get(name, '▪')} {html.escape(name)}</b>")
            continue
        if is_bullet:
            lines.append(html.escape("▪ " + s.lstrip("-•▪·*◦▫▸► ")))
        else:
            lines.append(html.escape(ln))
    return MULTI_NL_RE.sub("\n\n", "\n".join(lines).strip())


# ---------------------------------------------------------------- send
def _header(n_msgs, mode):
    now = datetime.now(KST)
    wd = "월화수목금토일"[now.weekday()]
    tag = " · 규칙모드" if mode == "rules" else ""
    return (f"<b>📰 텔레방 요약</b>  {now:%m/%d}({wd}) {now:%H:%M}{tag}\n"
            f"📥 수집 {n_msgs}건\n"
            "━━━━━━━━━━━━━━━━")


def _chunks(text, limit=TG_LIMIT):
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
        while len(cur) > limit:
            cut = cur.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(cur[:cut])
            cur = cur[cut:].lstrip("\n")
    if cur:
        parts.append(cur)
    return parts


def to_plain(html_text):
    return html.unescape(TAG_RE.sub("", html_text))


def send(body, n_msgs, mode):
    full = _header(n_msgs, mode) + "\n\n" + body
    parts = _chunks(full)
    n = len(parts)
    for i, p in enumerate(parts):
        if n > 1:
            p += f"\n\n<i>({i + 1}/{n})</i>"
        try:
            http_json(f"{API}/sendMessage", {
                "chat_id": TARGET_CHAT_ID, "text": p,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }, retries=1)
        except Exception as e:  # HTML 파싱 오류 등 → plain 재전송
            print(f"HTML send failed ({e}); resend as plain", file=sys.stderr)
            http_json(f"{API}/sendMessage", {
                "chat_id": TARGET_CHAT_ID, "text": to_plain(p),
                "disable_web_page_preview": True,
            })
        time.sleep(1)


def save_digest(summary_html):
    """sentiment_bot 연동: digests/YYYY-MM-DD.md 에 plain text로 저장 (최신 실행분을 맨 위에)."""
    try:
        os.makedirs("digests", exist_ok=True)
        path = f"digests/{datetime.now(KST).strftime('%Y-%m-%d')}.md"
        prev = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                prev = f.read()
        block = (f"## {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST 다이제스트\n\n"
                 f"{to_plain(summary_html)}\n\n")
        with open(path, "w", encoding="utf-8") as f:
            f.write(block + prev)
        print(f"digest saved: {path}")
    except Exception as e:
        print(f"digest save failed (발송에는 영향 없음): {e}", file=sys.stderr)


def main():
    state = load_state()
    messages = collect_messages(state)
    print(f"collected: {len(messages)} messages, offset={state['offset']}")

    if not messages:
        save_state(state)
        print("no new messages; skip sending")
        return

    mode = "rules"
    summary = None
    if GEMINI_API_KEY:
        try:
            summary, mode = format_gemini(summarize_gemini(messages)), "gemini"
        except Exception as e:
            print(f"gemini failed ({e}); fallback to rules", file=sys.stderr)
    if summary is None:
        summary = summarize_rules(messages)

    send(summary, len(messages), mode)
    save_digest(summary)
    save_state(state)
    print("done")


if __name__ == "__main__":
    main()
