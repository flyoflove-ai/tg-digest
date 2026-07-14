# -*- coding: utf-8 -*-
"""
텔레그램 그룹 메시지 수집 → 요약 → 발송 (하루 4회, 무료 스택) — v2

v2 변경점:
  - 이미지(사진) 요약 지원: Gemini 멀티모달로 이미지 내용까지 분석 (무료 티어 내)
  - 요약 상세도 강화: 항목별 2~4줄, 수치/목표주가/티커 보존, 출력 한도 확대
  - Gemini 모델명 404 대응: 여러 모델 자동 폴백

필요 Secrets: TELEGRAM_BOT_TOKEN / SOURCE_CHAT_ID / TARGET_CHAT_ID / GEMINI_API_KEY(선택)
"""

import base64
import json
import os
import re
import sys
import time
import urllib.request
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
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
STATE_FILE = "state.json"
KST = timezone(timedelta(hours=9))

MAX_IMAGES = 8            # 회당 Gemini에 넣을 이미지 최대 개수
MAX_IMAGE_BYTES = 4_000_000  # 이미지 1장 최대 크기 (약 4MB)

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
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode())
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
            elif photo:
                entry["text"] = (text + " [이미지 첨부 - 한도 초과로 미분석]").strip()

            messages.append(entry)
        if len(updates) < 100:
            break
    state["offset"] = offset
    print(f"images attached: {image_count}")
    return messages


# ---------------------------------------------------------------- summarize
PROMPT_HEADER = """당신은 한국 주식시장을 담당하는 시니어 애널리스트입니다.
아래는 투자 정보 텔레그램 방에 최근 올라온 메시지들입니다(텍스트 + 이미지).
이미지는 대부분 차트, 리포트 캡처, 뉴스 스크린샷, 표입니다. 이미지 안의 수치·종목명·
목표주가·표 내용까지 읽어서 요약에 반영하세요.

다음 형식으로 정리하세요:

1) 오늘의 핵심 (5~7줄)
   - 가장 중요한 내용을 우선순위대로. 각 줄에 근거 수치 포함

2) 섹터별 상세 정리 — 해당 내용이 있는 섹터만
   (반도체/메모리, 전력기기·그리드, 방산, 2차전지/소재, 로봇, 신재생, 매크로, 기타)
   - 섹터당 주요 항목 각각 2~4줄로 상세히
   - 수치, 목표주가, 증권사명, 날짜는 절대 생략하지 말 것
   - 팩트(발표/공시/수주)와 의견(전망/추정)을 구분해 표기

3) 언급 종목 리스트
   - 종목명: 언급 맥락 + 방향성(긍정/부정/중립) 한 줄씩

4) 주요 링크: URL + 한 줄 설명

규칙:
- 중복 내용은 합치되, 정보 손실 없이 통합
- 광고/잡담/인사만 제외하고 나머지는 최대한 보존
- 요약이 너무 짧아지지 않게: 원문 정보량의 골격이 유지되어야 함
- 텔레그램 발송용: 마크다운 특수문자(*, #, `) 없이 plain text + 이모지 불릿(▪, •)만 사용
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


def summarize_gemini(messages):
    parts = build_gemini_parts(messages)
    last_err = None
    for model in GEMINI_MODELS:
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


def summarize_rules(messages):
    """LLM 없이 폴백: 키워드 섹터 분류 + 링크 추출 (이미지는 건수만 표기)."""
    by_sector, links, n_img = defaultdict(list), [], 0
    for m in messages:
        if m["image"]:
            n_img += 1
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
    if n_img:
        lines.append(f"🖼 이미지 {n_img}건 포함 (규칙 기반 모드에서는 이미지 분석 불가)")
    for sector, msgs in by_sector.items():
        lines.append(f"\n📌 {sector} ({len(msgs)}건)")
        for m in msgs[:8]:
            head = m["text"].replace("\n", " ")[:160] or "[이미지]"
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
