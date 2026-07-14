# -*- coding: utf-8 -*-
"""
PDF 즉시 요약 봇 v2 — 자가진단 기능 포함
설정값이 잘못되면 어떤 Secret을 어떻게 고쳐야 하는지 로그에 한국어로 알려줌.

필요 Secrets: PDF_BOT_TOKEN / ALLOWED_CHAT_ID / GEMINI_API_KEY
"""

import base64
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
STATE_FILE = "state_pdf.json"
MAX_PDF_BYTES = 15_000_000

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]

PROMPT = """당신은 한국 주식시장을 담당하는 시니어 애널리스트입니다.
첨부된 PDF(증권사 리포트, 공시, 뉴스, IR 자료 등)를 읽고 아래 형식으로 요약하세요.

1) 문서 개요 (2~3줄): 발행 주체, 날짜, 문서 성격, 핵심 결론
2) 핵심 내용 (항목별 2~4줄, 상세하게)
   - 수치, 목표주가, 투자의견, 추정치(매출/영업이익/EPS), 밸류에이션(PER/PBR) 절대 생략 금지
   - 팩트(실적/공시/수주)와 의견(전망/추정)을 구분해 표기
3) 투자 포인트 & 리스크: 상방 요인과 하방 리스크 구분
4) 언급 종목/기업: 종목명 + 맥락 + 방향성(긍정/부정/중립)
5) 체크할 트리거/일정: 향후 이벤트, 실적 발표, 정책 일정

규칙: 표/차트 안의 숫자도 반영. 마크다운 특수문자(*, #, `) 없이
plain text + 이모지 불릿(▪, •)만 사용. 정보 골격이 유지되도록 충분히 상세하게."""


# ---------------------------------------------------------------- utils
def clean(v):
    """Secret 값의 공백/따옴표/줄바꿈 자동 제거."""
    return (v or "").strip().strip('"').strip("'").strip()


def die(msg):
    print("=" * 50)
    print(f"🔴 설정 오류: {msg}")
    print("=" * 50)
    sys.exit(1)


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


# ---------------------------------------------------------------- 자가진단
def validate_config():
    token = clean(os.environ.get("PDF_BOT_TOKEN"))
    chat_raw = clean(os.environ.get("ALLOWED_CHAT_ID"))
    gemini = clean(os.environ.get("GEMINI_API_KEY"))

    # 1) 토큰 존재 + 형식 검사
    if not token:
        die("PDF_BOT_TOKEN Secret이 비어있거나 등록되지 않았습니다.\n"
            "→ Settings > Secrets > Actions 에서 이름 철자까지 정확히 확인하세요.")
    if token.lower().startswith("bot"):
        token = token[3:]
        print("ℹ️ 토큰 앞의 'bot' 접두어를 자동 제거했습니다.")
    if not re.fullmatch(r"\d{8,12}:[A-Za-z0-9_-]{30,}", token):
        die("PDF_BOT_TOKEN 형식이 이상합니다.\n"
            "→ 정상 형식: 1234567890:AAHxxxxxxxxxxxx (콜론 포함 전체)\n"
            "→ BotFather에서 봇 선택 > API Token 으로 전체를 다시 복사하세요.")

    # 2) 토큰 유효성: getMe
    try:
        me = http_json(f"https://api.telegram.org/bot{token}/getMe", retries=1)
        bot_name = me["result"]["username"]
        print(f"✅ 봇 토큰 정상: @{bot_name}")
    except Exception:
        die("PDF_BOT_TOKEN이 텔레그램에서 거부되었습니다 (404/401).\n"
            "→ 토큰이 일부만 복사되었거나, revoke로 무효화된 옛 토큰입니다.\n"
            "→ BotFather > 봇 선택 > API Token 에서 최신 토큰 전체를 재복사해\n"
            "  Secret을 Update 하세요.")

    # 3) chat_id 검사
    if not chat_raw:
        die("ALLOWED_CHAT_ID Secret이 비어있거나 등록되지 않았습니다.")
    chat_digits = re.sub(r"[^\d-]", "", chat_raw)
    if not chat_digits or not chat_digits.lstrip("-").isdigit():
        die(f"ALLOWED_CHAT_ID가 숫자가 아닙니다.\n"
            "→ 본인 텔레그램 ID(양수 숫자)만 넣으세요. 예: 123456789")
    chat_id = int(chat_digits)
    if chat_id < 0:
        die("ALLOWED_CHAT_ID가 음수(그룹 ID)입니다.\n"
            "→ PDF 봇은 본인 DM용이므로 양수인 본인 ID를 넣어야 합니다.\n"
            "  (digest의 TARGET_CHAT_ID와 같은 값)")
    print(f"✅ ALLOWED_CHAT_ID 정상: {chat_id}")

    # 4) Gemini 키
    if not gemini:
        die("GEMINI_API_KEY Secret이 비어있습니다.\n"
            "→ https://aistudio.google.com 에서 발급한 키를 등록하세요.")
    print("✅ GEMINI_API_KEY 존재 확인")

    return token, chat_id, gemini


# ---------------------------------------------------------------- core
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"offset": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def summarize_pdf(gemini_key, pdf_b64, filename):
    parts = [
        {"text": PROMPT + f"\n\n파일명: {filename}"},
        {"inline_data": {"mime_type": "application/pdf", "data": pdf_b64}},
    ]
    last_err = None
    for model in GEMINI_MODELS:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={gemini_key}")
        try:
            res = http_json(url, {
                "contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0.3,
                                     "maxOutputTokens": 8192},
            }, retries=1)
            print(f"✅ gemini model used: {model}")
            return res["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            last_err = e
            continue
    raise last_err


def is_pdf(doc):
    if not doc:
        return False
    return (doc.get("mime_type") == "application/pdf"
            or (doc.get("file_name") or "").lower().endswith(".pdf"))


def main():
    token, allowed_id, gemini_key = validate_config()
    api = f"https://api.telegram.org/bot{token}"
    file_api = f"https://api.telegram.org/file/bot{token}"

    def reply(chat_id, text):
        for i in range(0, len(text), 3900):
            http_json(f"{api}/sendMessage", {
                "chat_id": chat_id, "text": text[i:i + 3900],
                "disable_web_page_preview": True,
            })
            time.sleep(1)

    state = load_state()
    offset = state.get("offset", 0)
    processed = 0

    while True:
        res = http_json(f"{api}/getUpdates",
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
            if chat_id != allowed_id:
                print(f"ℹ️ 허용되지 않은 chat_id({chat_id})의 메시지 무시")
                continue

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
                info = http_json(f"{api}/getFile", {"file_id": doc["file_id"]})
                raw = http_bytes(f"{file_api}/{info['result']['file_path']}")
                summary = summarize_pdf(gemini_key,
                                        base64.b64encode(raw).decode(), fname)
                header = (f"📑 PDF 요약: {fname}\n"
                          f"🕐 {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST\n"
                          + "─" * 20 + "\n")
                reply(chat_id, header + summary)
                processed += 1
            except Exception as e:
                print(f"🔴 pdf processing failed: {e}", file=sys.stderr)
                reply(chat_id, f"🔴 {fname} 요약 실패. 잠시 후 다시 보내보세요.")

        if len(updates) < 100:
            break

    state["offset"] = offset
    save_state(state)
    print(f"✅ 완료: PDF {processed}건 처리, offset={offset}")


if __name__ == "__main__":
    main()
