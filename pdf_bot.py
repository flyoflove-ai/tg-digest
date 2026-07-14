# -*- coding: utf-8 -*-
"""
PDF 즉시 요약 봇 v4
  - 모델 자동 탐색: 실행 시 ListModels로 내 키가 쓸 수 있는 Flash 모델을 조회해
    최신순으로 자동 시도. 구글이 모델 라인업을 바꿔도 코드 수정 불필요.
  - 할당량 0인 모델(limit: 0)은 대기 없이 즉시 건너뜀
  - 대용량 PDF: 4MB 초과 시 Files API 업로드 방식

필요 Secrets: PDF_BOT_TOKEN / ALLOWED_CHAT_ID / GEMINI_API_KEY
선택 Secret:  GEMINI_MODEL (특정 모델 강제 지정 시)
"""

import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
STATE_FILE = "state_pdf.json"
MAX_PDF_BYTES = 18_000_000
INLINE_THRESHOLD = 4_000_000

GEMINI_BASE = "https://generativelanguage.googleapis.com"

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


# ---------------------------------------------------------------- http
def _do_request(url, data=None, headers=None, timeout=300, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:800]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {e.reason} | {body}") from None


def http_json(url, payload=None, headers=None, retries=3, timeout=300):
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    last = None
    for i in range(retries):
        try:
            raw, _ = _do_request(url, data, hdrs, timeout)
            return json.loads(raw.decode())
        except RuntimeError as e:
            last = e
            msg = str(e)
            if "HTTP 429" in msg:
                if "limit: 0" in msg:      # 할당량 자체가 0 → 재시도 무의미
                    raise
                print("⏳ 429 rate limit, 35초 대기 후 재시도")
                time.sleep(35)
                continue
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))
        except Exception as e:
            last = e
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))
    raise last


def http_get_json(url):
    raw, _ = _do_request(url, timeout=60)
    return json.loads(raw.decode())


def http_bytes(url):
    raw, _ = _do_request(url, timeout=300)
    return raw


# ---------------------------------------------------------------- config
def clean(v):
    return (v or "").strip().strip('"').strip("'").strip()


def die(msg):
    print("=" * 50)
    print(f"🔴 설정 오류: {msg}")
    print("=" * 50)
    sys.exit(1)


def validate_config():
    token = clean(os.environ.get("PDF_BOT_TOKEN"))
    chat_raw = clean(os.environ.get("ALLOWED_CHAT_ID"))
    gemini = clean(os.environ.get("GEMINI_API_KEY"))

    if not token:
        die("PDF_BOT_TOKEN Secret이 비어있습니다.")
    if token.lower().startswith("bot"):
        token = token[3:]
    if not re.fullmatch(r"\d{8,12}:[A-Za-z0-9_-]{30,}", token):
        die("PDF_BOT_TOKEN 형식이 이상합니다. BotFather에서 전체를 재복사하세요.")
    try:
        me = http_json(f"https://api.telegram.org/bot{token}/getMe", retries=1)
        print(f"✅ 봇 토큰 정상: @{me['result']['username']}")
    except Exception as e:
        die(f"PDF_BOT_TOKEN이 거부되었습니다: {e}")

    chat_digits = re.sub(r"[^\d-]", "", chat_raw)
    if not chat_digits.lstrip("-").isdigit():
        die("ALLOWED_CHAT_ID가 숫자가 아닙니다. 본인 ID(양수)만 넣으세요.")
    chat_id = int(chat_digits)
    if chat_id < 0:
        die("ALLOWED_CHAT_ID가 음수(그룹 ID)입니다. 본인 ID(양수)를 넣으세요.")
    print(f"✅ ALLOWED_CHAT_ID 정상: {chat_id}")

    if not gemini:
        die("GEMINI_API_KEY Secret이 비어있습니다.")
    print("✅ GEMINI_API_KEY 존재 확인")
    return token, chat_id, gemini


# ---------------------------------------------------------------- 모델 자동 탐색
def _version_key(model_id):
    """모델명에서 버전 숫자 추출 → 최신 우선 정렬용. 예: gemini-3.1-flash → 3.1"""
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", model_id)
    return float(m.group(1)) if m else 0.0


def discover_models(key):
    """내 키로 쓸 수 있는 generateContent 지원 flash 모델을 최신순으로 반환."""
    forced = clean(os.environ.get("GEMINI_MODEL"))
    models, page_token = [], ""
    try:
        while True:
            url = f"{GEMINI_BASE}/v1beta/models?key={key}&pageSize=200"
            if page_token:
                url += f"&pageToken={page_token}"
            res = http_get_json(url)
            models += res.get("models", [])
            page_token = res.get("nextPageToken", "")
            if not page_token:
                break
    except Exception as e:
        print(f"⚠️ 모델 목록 조회 실패({e}) → 기본 후보로 진행", file=sys.stderr)
        fallback = ["gemini-3-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash"]
        return ([forced] if forced else []) + fallback

    usable = []
    for m in models:
        mid = m.get("name", "").replace("models/", "")
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" not in methods:
            continue
        if "flash" not in mid:
            continue
        # 텍스트 요약에 부적합한 변형 제외
        if any(x in mid for x in ("image", "tts", "audio", "live",
                                  "embedding", "exp", "thinking")):
            continue
        usable.append(mid)

    # 최신 버전 우선, 같은 버전이면 일반(flash) > lite
    usable.sort(key=lambda x: (-_version_key(x), "lite" in x, len(x)))
    if forced and forced in usable:
        usable.remove(forced)
    ordered = ([forced] if forced else []) + usable[:6]
    print(f"🔎 사용 가능 모델 (시도 순서): {ordered}")
    if not ordered:
        raise RuntimeError("이 키로 쓸 수 있는 flash 모델이 없습니다. "
                           "https://aistudio.google.com 에서 키 상태를 확인하세요.")
    return ordered


# ---------------------------------------------------------------- gemini
def gemini_upload(key, raw, filename):
    start_headers = {
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(raw)),
        "X-Goog-Upload-Header-Content-Type": "application/pdf",
        "Content-Type": "application/json",
    }
    body = json.dumps({"file": {"display_name": filename}}).encode()
    _, headers = _do_request(
        f"{GEMINI_BASE}/upload/v1beta/files?key={key}", body, start_headers)
    upload_url = headers.get("X-Goog-Upload-URL") or headers.get("x-goog-upload-url")
    if not upload_url:
        raise RuntimeError("Files API 업로드 URL을 받지 못했습니다.")

    up_headers = {
        "X-Goog-Upload-Offset": "0",
        "X-Goog-Upload-Command": "upload, finalize",
        "Content-Length": str(len(raw)),
    }
    resp_raw, _ = _do_request(upload_url, raw, up_headers)
    finfo = json.loads(resp_raw.decode())["file"]
    name, uri, state = finfo["name"], finfo["uri"], finfo.get("state", "")
    print(f"✅ Files API 업로드 완료: {name} (state={state})")

    waited = 0
    while state == "PROCESSING" and waited < 120:
        time.sleep(5)
        waited += 5
        st = http_get_json(f"{GEMINI_BASE}/v1beta/{name}?key={key}")
        state = st.get("state", "")
    if state not in ("ACTIVE", ""):
        raise RuntimeError(f"업로드 파일 처리 실패 (state={state})")
    return uri


def extract_text(res):
    cands = res.get("candidates") or []
    if not cands:
        raise RuntimeError(
            f"응답에 candidates 없음: {json.dumps(res, ensure_ascii=False)[:400]}")
    parts = cands[0].get("content", {}).get("parts", [])
    text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
    if not text:
        fr = cands[0].get("finishReason", "?")
        raise RuntimeError(f"응답에 텍스트 없음 (finishReason={fr})")
    return text


def summarize_pdf(key, models, raw, filename):
    if len(raw) > INLINE_THRESHOLD:
        print(f"ℹ️ 대용량 PDF ({len(raw)/1e6:.1f}MB) → Files API 사용")
        uri = gemini_upload(key, raw, filename)
        pdf_part = {"file_data": {"mime_type": "application/pdf",
                                  "file_uri": uri}}
    else:
        pdf_part = {"inline_data": {"mime_type": "application/pdf",
                                    "data": base64.b64encode(raw).decode()}}

    parts = [{"text": PROMPT + f"\n\n파일명: {filename}"}, pdf_part]
    last_err = None
    for model in models:
        try:
            res = http_json(
                f"{GEMINI_BASE}/v1beta/models/{model}:generateContent?key={key}",
                {"contents": [{"parts": parts}],
                 "generationConfig": {"temperature": 0.3,
                                      "maxOutputTokens": 8192}},
                retries=2)
            print(f"✅ gemini model used: {model}")
            return extract_text(res)
        except Exception as e:
            print(f"⚠️ {model} 실패: {str(e)[:300]}", file=sys.stderr)
            last_err = e
            continue
    raise last_err


# ---------------------------------------------------------------- state
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"offset": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_pdf(doc):
    if not doc:
        return False
    return (doc.get("mime_type") == "application/pdf"
            or (doc.get("file_name") or "").lower().endswith(".pdf"))


# ---------------------------------------------------------------- main
def main():
    token, allowed_id, gemini_key = validate_config()
    models = discover_models(gemini_key)
    api = f"https://api.telegram.org/bot{token}"
    file_api = f"https://api.telegram.org/file/bot{token}"

    def reply(chat_id, text):
        for i in range(0, len(text), 3900):
            http_json(f"{api}/sendMessage", {
                "chat_id": chat_id, "text": text[i:i + 3900],
                "disable_web_page_preview": True})
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
                continue

            doc = msg.get("document")
            if not is_pdf(doc):
                if msg.get("text"):
                    reply(chat_id, "📄 PDF 파일을 첨부해서 보내주시면 요약해드립니다.\n"
                                   "(최대 18MB, 답장까지 보통 5~15분 소요)")
                continue

            fname = doc.get("file_name", "문서.pdf")
            if doc.get("file_size", 0) > MAX_PDF_BYTES:
                reply(chat_id, f"⚠️ {fname}: 18MB를 초과해 처리할 수 없습니다.")
                continue

            try:
                info = http_json(f"{api}/getFile", {"file_id": doc["file_id"]})
                raw = http_bytes(f"{file_api}/{info['result']['file_path']}")
                print(f"📥 다운로드 완료: {fname} ({len(raw)/1e6:.1f}MB)")
                summary = summarize_pdf(gemini_key, models, raw, fname)
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
