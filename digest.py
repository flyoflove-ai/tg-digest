# -*- coding: utf-8 -*-
"""
텔레그램 그룹 메시지 수집 → 요약 → 발송 (하루 4회, 완전 무료 스택)

무료 구성:
  - 수집/발송: Telegram Bot API (무료)
  - 실행:      GitHub Actions cron (무료 한도 내)
  - 요약:      Google Gemini API 무료 티어 (GEMINI_API_KEY 있을 때)
               없으면 규칙 기반 요약으로 자동 폴백 (완전 무료, LLM 없음)

필요 Secrets:
  TELEGRAM_BOT_TOKEN : 이 용도 전용 봇 토큰 (기존 리서치 에이전트 봇과 분리 권장)
  SOURCE_CHAT_ID     : 수집 대상 그룹 chat_id (예: -1001234567890)
  TARGET_CHAT_ID     : 요약 리포트를 받을 chat_id (본인 DM 등)
  GEMINI_API_KEY     : (선택) 없으면 규칙 기반 요약
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone, timedelta

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
TARGET_CHAT_ID = os.environ["TARGET_CHAT_ID"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "")] if m] + [
    "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash",
]

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_FILE = "state.json"
KST = timezone(timedelta(hours=9))

SECTOR_KEYWORDS = {
    "반도체/메모리": ["반도체", "하이닉스", "삼성전자", "HBM", "DRAM", "낸드", "NAND",
                  "파운드리", "TSMC", "엔비디아", "Nvidia", "CoWoS", "웨이퍼", "소부장",
                  "마이크론", "Micron", "CAPEX", "캐펙스"],
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

URL_RE = re.compile(r"https?://\S+")


# ---------------------------------------------------------------- utils
def http_json(url, payload=None, headers=None, retries=3):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"offset": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- collect
def collect_messages(state):
    """getUpdates 증분 수집. offset 체크포인트로 중복 방지."""
    messages, offset = [], state.get("offset", 0)
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
            text = msg.get("text") or msg.get("caption") or ""
            if not text.strip():
                continue
            sender = (msg.get("from", {}).get("first_name")
                      or msg.get("author_signature") or "")
            ts = datetime.fromtimestamp(msg["date"], tz=KST)
            messages.append({"time": ts.strftime("%m/%d %H:%M"),
                             "sender": sender, "text": text.strip()})
        if len(updates) < 100:
            break
    state["offset"] = offset
    return messages


# ---------------------------------------------------------------- summarize
def summarize_gemini(messages):
    corpus = "\n\n".join(
        f"[{m['time']}] {m['sender']}: {m['text']}"[:1500] for m in messages
    )[:100_000]
    prompt = f"""당신은 한국 주식시장을 담당하는 시니어 애널리스트입니다.
아래는 투자 정보 텔레그램 방에 최근 올라온 메시지들입니다. 다음 형식으로 요약하세요.

1) 핵심 요약: 3~5줄, 가장 중요한 내용 위주
2) 섹터별 정리: 반도체/메모리, 전력기기·그리드, 방산, 2차전지/소재, 로봇, 신재생, 매크로, 기타
   - 해당 내용이 있는 섹터만 포함
3) 언급 종목: 종목명과 언급 맥락 한 줄씩
4) 주요 링크: URL과 한 줄 설명

규칙: 사실과 의견을 구분하고, 중복 내용은 합치고, 광고/잡담은 제외.
텔레그램 발송용이므로 마크다운 특수문자 없이 plain text + 이모지 불릿(▪, •)만 사용.

--- 메시지 시작 ---
{corpus}
--- 메시지 끝 ---"""

    last_err = None
    for model in GEMINI_MODELS:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={GEMINI_API_KEY}")
        try:
            res = http_json(url, {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3,
                                     "maxOutputTokens": 2048},
            }, retries=1)
            print(f"gemini model used: {model}")
            return res["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            last_err = e
            continue
    raise last_err


def summarize_rules(messages):
    """LLM 없이 완전 무료 폴백: 키워드 섹터 분류 + 링크 추출."""
    by_sector, links = defaultdict(list), []
    for m in messages:
        for u in URL_RE.findall(m["text"]):
            links.append(u)
        tagged = False
        for sector, kws in SECTOR_KEYWORDS.items():
            if any(k.lower() in m["text"].lower() for k in kws):
                by_sector[sector].append(m)
                tagged = True
                break
        if not tagged:
            by_sector["기타"].append(m)

    lines = []
    for sector, msgs in by_sector.items():
        lines.append(f"\n📌 {sector} ({len(msgs)}건)")
        for m in msgs[:8]:
            head = m["text"].replace("\n", " ")[:120]
            lines.append(f"  ▪ [{m['time']}] {head}")
        if len(msgs) > 8:
            lines.append(f"  … 외 {len(msgs) - 8}건")
    if links:
        lines.append(f"\n🔗 링크 {len(links)}건")
        for u in list(dict.fromkeys(links))[:10]:
            lines.append(f"  • {u}")
    return "\n".join(lines)


# ---------------------------------------------------------------- send
def send(text):
    header = (f"📰 텔레방 요약 리포트\n"
              f"🕐 {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST\n"
              + "─" * 20 + "\n")
    full = header + text
    for i in range(0, len(full), 3900):
        http_json(f"{API}/sendMessage", {
            "chat_id": TARGET_CHAT_ID,
            "text": full[i:i + 3900],
            "disable_web_page_preview": True,
        })
        time.sleep(1)


def main():
    state = load_state()
    messages = collect_messages(state)
    print(f"collected: {len(messages)} messages, offset={state['offset']}")

    if not messages:
        save_state(state)
        print("no new messages; skip sending")
        return

    if GEMINI_API_KEY:
        try:
            summary = summarize_gemini(messages)
        except Exception as e:
            print(f"gemini failed ({e}); fallback to rules", file=sys.stderr)
            summary = summarize_rules(messages)
    else:
        summary = summarize_rules(messages)

    send(summary)
    save_state(state)
    print("done")


if __name__ == "__main__":
    main()
