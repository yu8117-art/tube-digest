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

`remove`는 이름(앞의 `@`, 대소문자, 띄어쓰기는 무시), 이름 일부, `@핸들`, 채널 주소, `UC…` ID 중 아무거나 받는다. 예를 들어 `.\td remove "유안타"`처럼 이름 일부만 쳐도 된다. 다만 이름 일부가 여러 채널에 걸리면 지우지 않고 후보만 보여준다.

- PowerShell에서는 `@핸들`을 반드시 따옴표로 감싼다. 따옴표가 없으면 PowerShell이 `@이름`을 변수 펼치기 문법으로 해석해 인자가 사라진다.
- `add` / `remove` / `list`는 GitHub에서 최신 채널 목록을 먼저 받아온다. `add`와 `remove`는 바뀐 `channels.yaml`만 커밋해서 GitHub에 올리므로, 다음 Actions 실행(15분 안팎)부터 바로 적용된다. 따로 `git` 명령을 칠 필요가 없다.
- `td`는 `digest` 폴더 안에 있다. 다른 폴더에서는 `C:\DEV\tube\digest\td add "@핸들"`처럼 경로를 붙인다.

## 텔레그램으로 링크 보내서 요약 받기

텔레그램 봇 대화방에 유튜브 링크를 보내면 요약해서 그 메시지에 답장한다. 구독하지 않은 채널의 영상도 된다.

- 보내는 방법: 유튜브 앱의 **공유 → 텔레그램 → 봇 대화방**, 또는 링크를 붙여넣기. 한 메시지에 링크 여러 개도 된다(최대 3개).
- 답장까지 보통 **5~15분** 걸린다. GitHub Actions(`.github/workflows/telegram.yml`)가 5분마다 새 메시지를 확인하기 때문이다. 먼저 "요약 중" 답장이 오고, 요약이 끝나면 요약 메시지가 온다.
- 이미 요약한 영상이면 Gemini를 다시 쓰지 않고 저장된 요약을 보낸다.
- 등록한 채팅(`TELEGRAM_CHAT_ID`)에서 온 메시지만 처리한다. 다른 사람이 봇에 링크를 보내도 무시한다.
- 바로 처리하고 싶으면 저장소의 Actions → **telegram-requests → Run workflow**를 누른다.

## 명령어

| 명령 | 설명 |
|---|---|
| `run` | 새 영상 확인 → 요약 → 알림 (Actions가 15분마다 호출). PC에서는 돌리지 않는다 |
| `summarize <url>` | 특정 영상을 바로 요약해 화면에 출력. `--send`를 붙이면 텔레그램으로도 보낸다 |
| `notify` | 실패한 알림 재전송 |
| `telegram` | 텔레그램으로 받은 링크 요약·답장 (Actions가 5분마다 호출) |
| `add` / `remove` / `list` | 채널 관리 |
| `test-notify` | 가장 최근 요약을 텔레그램으로 다시 보내 설정 확인 |
| `telegram-chat-id` | 텔레그램 chat_id 확인 |

## 자주 바꾸는 설정

- 요약 지시문: `prompts/summary_prompt.md` (push하면 다음 요약부터 반영)
- 쇼츠 포함 여부: `config.yaml`의 `options.skip_shorts`
- Gemini 모델과 긴 영상 구간 길이: `config.yaml`의 `gemini` 항목
- 알림 방식(telegram / ntfy): `config.yaml`의 `notify.provider`
