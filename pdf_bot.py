# -*- coding: utf-8 -*-
"""
PDF 즉시 요약 봇 (무료 스택) — 별도 전용 봇으로 운영

동작:
  - 봇 DM으로 PDF 파일을 보내면 → Gemini(무료 티어)가 PDF를 직접 읽고 요약 → 답장
  - 10분 간격 폴링 (GitHub Actions cron). 즉시는 아니고 보통 5~15분 내 답장
  - 텍스트 PDF뿐 아니라 스캔본/이미지형 PDF도 Gemini가 직접 읽음 (별도 OCR 불필요)

기존 digest 봇과 반드시 다른 봇 토큰을 사용해야 함 (getUpdates 충돌 방지).

필요 Secrets:
  PDF_BOT_TOKEN   : PDF 요약 전용 신규 봇 토큰
  ALLOWED_CHAT_ID : 본인 텔레그램 ID (이 ID 외의 요청은 무시 - 무료 한도 보호)
  GEMINI_API_KEY  : Gemini 키 (digest와 공용)
"""

import base64
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

BOT_TOKEN = os.environ["PDF_BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

GEMINI_MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "")] if m] + [
    "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash",
]

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
STATE_FILE = "state_pdf.json"
KST = timezone(timedelta(hours=9))

MAX_PDF_BYTES = 15_000_000  # 15MB (텔레그램 봇 다운로드 한도 20MB 이내)

PROMPT = """당신은 한국 주식시장을 담당하는 시니어 애널리스트입니다.
첨부된 PDF(증권사 리포트, 공시, 뉴스, IR 자료 등)를 읽고 아래 형식으로 요약하세요.

1) 문서 개요 (2~3줄): 발행 주체, 날짜, 문서 성격, 핵심 결론

2) 핵심 내용 (항목별 2~4줄, 상세하게)
   - 수치, 목표주가, 투자의견, 추정치(매출/영업이익/EPS), 밸류에이션(PER/PBR) 절대 생략 금지
   - 팩트(실적/공시/수주)와 의견(전망/추정)을 구분해 표기

3) 투자 포인트 & 리스크
   - 상방 요인과 하방 리스크를 각각 구분

4) 언급 종목/기업: 종목명 + 맥락 + 방향성(긍정/부정/중립)

5) 체크할 트리거/일정: 문서에 언급된 향후 이벤트, 실적 발표, 정책 일정

규칙:
- 표와 차트 안의 숫자도 읽어서 반영
- 텔레그램 발송용: 마크다운 특수문자(*, #, `) 없이 plain text + 이모지 불릿(▪, •)만 사용
- 문서가 길어도 정보 골격이 유지되도록 충분히 상세하게"""


# ---------------------------------------------------------------- utils
def http_json(url, payload=None, retries=3):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def http_bytes(url):
    with urllib.request.urlopen(url, timeout=180) as r:
        return r.read()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"offset": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def reply(chat_id, text):
    for i in range(0, len(text), 3900):
        http_json(f"{API}/sendMessage", {
            "chat_id": chat_id, "text": text[i:i + 3900],
            "disable_web_page_preview": True,
        })
        time.sleep(1)


# ---------------------------------------------------------------- gemini
def summarize_pdf(pdf_b64, filename):
    parts = [
        {"text": PROMPT + f"\n\n파일명: {filename}"},
        {"inline_data": {"mime_type": "application/pdf", "data": pdf_b64}},
    ]
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


# ---------------------------------------------------------------- main
def is_pdf(doc):
    if not doc:
        return False
    return (doc.get("mime_type") == "application/pdf"
            or (doc.get("file_name") or "").lower().endswith(".pdf"))


def main():
    state = load_state()
    offset = state.get("offset", 0)
    processed = 0

    while True:
        res = http_json(f"{API}/getUpdates",
                        {"offset": offset + 1, "timeout": 0, "limit": 100,
                         "allowed_updates": ["message"]})
        updates = res.get("result", [])
        if not updates:
            break
        for u in updates:
            offset = max(offset, u["update_id"])
            msg = u.get("message")
            if not msg:
                continue
            chat_id = msg.get("chat", {}).get("id")
            if chat_id != ALLOWED_CHAT_ID:
                continue  # 본인 외 요청 무시 (무료 한도 보호)

            doc = msg.get("document")
            if not is_pdf(doc):
                if msg.get("text"):
                    reply(chat_id, "📄 PDF 파일을 첨부해서 보내주시면 요약해드립니다.\n"
                                   "(최대 15MB, 답장까지 보통 5~15분 소요)")
                continue

            fname = doc.get("file_name", "문서.pdf")
            if doc.get("file_size", 0) > MAX_PDF_BYTES:
                reply(chat_id, f"⚠️ {fname}: 15MB를 초과해 처리할 수 없습니다.")
                continue

            try:
                info = http_json(f"{API}/getFile", {"file_id": doc["file_id"]})
                raw = http_bytes(f"{FILE_API}/{info['result']['file_path']}")
                summary = summarize_pdf(base64.b64encode(raw).decode(), fname)
                header = (f"📑 PDF 요약: {fname}\n"
                          f"🕐 {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST\n"
                          + "─" * 20 + "\n")
                reply(chat_id, header + summary)
                processed += 1
            except Exception as e:
                print(f"pdf processing failed: {e}", file=sys.stderr)
                reply(chat_id, f"🔴 {fname} 요약 실패. 잠시 후 다시 보내보세요.")

        if len(updates) < 100:
            break

    state["offset"] = offset
    save_state(state)
    print(f"processed PDFs: {processed}, offset={offset}")


if __name__ == "__main__":
    main()
