# Tube Digest

지정한 유튜브 채널에 새 영상이 올라오면 Gemini가 영상을 보고 요약해서, 요약 전문을 텔레그램으로 보낸다.
GitHub Actions가 15분마다 돌리므로 PC를 켜 둘 필요가 없다. 구조와 설계 근거는 [DESIGN.md](DESIGN.md)에 있다.

## 필요한 키 (GitHub Secrets와 로컬 `.env`에 같은 이름으로)

| 이름 | 발급 |
|---|---|
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey (무료) |
| `YOUTUBE_API_KEY` | Google Cloud Console → API 및 서비스 → 라이브러리에서 **YouTube Data API v3** 사용 설정 → 사용자 인증 정보 → API 키 만들기 (무료). Gemini 키로는 안 된다 |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 **@BotFather**에게 `/newbot` |
| `TELEGRAM_CHAT_ID` | 봇에게 `/start`를 보낸 뒤 `.\td telegram-chat-id` |

GitHub 저장소 **Settings → Secrets and variables → Actions**에 등록한다. 로컬에서 쓸 때는 `.env.example`을 `.env`로 복사해 채운다.

## 로컬 설치 (Windows, 최초 1회)

```powershell
cd C:\DEV\tube\digest
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

모든 명령은 `digest` 폴더에서 `.\td <명령>`으로 실행한다. `python -m tubedigest <명령>`을 써도 가상환경으로 알아서 다시 실행된다.

## 채널 관리

```powershell
.\td add "@syukaworld" "@보다BODA"
.\td list
.\td remove "보다 BODA"
```

- PowerShell에서는 `@핸들`을 반드시 따옴표로 감싼다. 따옴표가 없으면 PowerShell이 `@이름`을 변수 펼치기 문법으로 해석해 인자가 사라진다.
- 바꾼 `channels.yaml`은 커밋·push해야 GitHub Actions에 반영된다.

## 명령어

| 명령 | 설명 |
|---|---|
| `run` | 새 영상 확인 → 요약 → 알림 (Actions가 15분마다 호출). PC에서는 돌리지 않는다 |
| `summarize <url>` | 특정 영상을 바로 요약해 화면에 출력. `--send`를 붙이면 텔레그램으로도 보낸다 |
| `notify` | 실패한 알림 재전송 |
| `add` / `remove` / `list` | 채널 관리 |
| `test-notify` | 가장 최근 요약을 텔레그램으로 다시 보내 설정 확인 |
| `telegram-chat-id` | 텔레그램 chat_id 확인 |

## 자주 바꾸는 설정

- 요약 지시문: `prompts/summary_prompt.md` (push하면 다음 요약부터 반영)
- 쇼츠 포함 여부: `config.yaml`의 `options.skip_shorts`
- Gemini 모델과 긴 영상 구간 길이: `config.yaml`의 `gemini` 항목
- 알림 방식(telegram / ntfy): `config.yaml`의 `notify.provider`
