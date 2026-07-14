# 텔레방 요약 봇 (무료 스택, 하루 4회)

Telegram Bot API + GitHub Actions + Gemini 무료 티어(선택). 비용 0원.

## 1. 전용 봇 만들기 (기존 리서치 에이전트 봇과 반드시 분리)

기존 봇을 재사용하면 `getUpdates` 소비 충돌로 두 시스템 모두 메시지가 유실됩니다.

1. @BotFather → `/newbot` → 토큰 발급
2. @BotFather → `/setprivacy` → 새 봇 선택 → **Disable**
   (이걸 꺼야 그룹의 모든 메시지를 봇이 볼 수 있음. 기본값은 명령어만 수신)
3. 봇을 대상 그룹에 멤버로 초대

## 2. chat_id 확인

그룹에서 아무 메시지나 하나 보낸 뒤 브라우저에서:

```
https://api.telegram.org/bot<토큰>/getUpdates
```

- `SOURCE_CHAT_ID`: 그룹 chat.id (보통 `-100`으로 시작하는 음수)
- `TARGET_CHAT_ID`: 리포트 받을 곳. 본인 DM이면 봇에게 먼저 아무 말이나 보낸 뒤 같은 방법으로 확인

## 3. GitHub 설정

1. **Private 저장소** 생성 (봇 토큰이 secrets에 있어도 private 권장.
   무료 한도: private 2,000분/월 — 이 워크플로는 월 ~120분 사용)
2. 이 폴더 내용 전체 push
3. Settings → Secrets and variables → Actions:

| Secret | 값 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 새 봇 토큰 |
| `SOURCE_CHAT_ID` | 수집 대상 그룹 ID |
| `TARGET_CHAT_ID` | 리포트 수신 ID |
| `GEMINI_API_KEY` | (선택) https://aistudio.google.com 에서 무료 발급 |

4. Actions 탭 → `Telegram Digest` → **Run workflow** 로 첫 테스트

## 4. 요약 엔진 (2단 구조)

- `GEMINI_API_KEY` 있음 → Gemini 2.5 Flash 무료 티어로 애널리스트 스타일 요약
  (섹터별 정리 + 언급 종목 + 링크). 하루 4회 호출은 무료 한도 대비 극히 미미
- 키 없음 or 호출 실패 → **규칙 기반 폴백**: 키워드 섹터 분류(반도체/전력/방산/2차전지/로봇/신재생/매크로) + 링크 추출. LLM 없이 완전 무료

## 5. 알아둘 제약

- 봇은 **초대된 시점 이후** 메시지만 수신 (과거 히스토리 불가)
- Telegram은 미수신 업데이트를 **24시간만 보관** → 하루 4회 스케줄(최대 간격 10시간)이면 안전. 워크플로를 며칠 꺼두면 그 사이 메시지는 유실
- 봇이 그룹에서 강퇴되면 수집 중단 → 실패 알림은 오지 않으니(에러가 아니라 빈 수신) 리포트가 안 오면 봇 멤버십 확인
- `state.json`(offset 체크포인트)은 매 실행 후 자동 커밋됨

## 스케줄 변경

`.github/workflows/telegram_digest.yml`의 cron 수정. UTC 기준이므로 KST − 9시간.
현재: 08:00 / 12:00 / 17:00 / 22:00 KST (장전 / 장중 / 장마감 / 미장 전)
