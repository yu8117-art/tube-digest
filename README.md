# Tube Digest

지정한 유튜브 채널의 새 영상을 자동으로 요약해 모바일 매거진 기사로 만들고 폰으로 알림을 보낸다.
구조와 설계 근거는 [DESIGN.md](DESIGN.md)에 있다.

## 설치 (Windows, 최초 1회)

```powershell
cd C:\DEV\tube\digest
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

`notepad .env`로 열어 `ANTHROPIC_API_KEY`를 넣는다. 키는 https://console.anthropic.com 에서 발급한다.

> **실행 방법**: 모든 명령은 `digest` 폴더에서 `.\td <명령>`으로 실행한다. 다른 폴더에서는 `C:\DEV\tube\digest\td <명령>`으로 실행한다.
> 그냥 `python -m tubedigest`로 실행하면 가상환경이 아닌 시스템 Python이 잡혀서 `No module named ...` 오류가 난다.

## 1. 채널 등록

```powershell
.\td add "@syukaworld" "@보다BODA"
.\td list
```

- `@핸들`, 채널 URL, `UC…` ID 중 아무거나 넣으면 된다. `channels.yaml`을 직접 편집해도 된다.
- **PowerShell에서는 `@핸들`을 반드시 따옴표로 감싼다.** 따옴표가 없으면 PowerShell이 `@이름`을 변수 펼치기 문법으로 해석해서 인자가 사라진다. `@` 없이 `보다BODA`라고만 써도 된다.

## 2. 폰 알림 (Telegram)

1. 텔레그램에서 **@BotFather**에게 `/newbot`을 보내 봇을 만들고, 받은 토큰을 `.env`의 `TELEGRAM_BOT_TOKEN`에 넣는다.
2. 방금 만든 봇과 대화방을 열고 `/start`를 보낸다.
3. 아래 명령으로 chat_id를 확인한 뒤 `.env`의 `TELEGRAM_CHAT_ID`에 넣는다.
   ```powershell
   .\td telegram-chat-id
   ```
4. `.\td test-notify`를 실행해 폰에 알림이 오는지 확인한다.

## 2-1. 영상 분석 키 (Gemini, 무료)

GitHub Actions에서는 유튜브가 자막 요청을 막는다. 그때 Gemini가 영상을 직접 보고 시간대별 노트를 만들어, 자막이 있을 때와 같은 프롬프트로 요약한다.

1. https://aistudio.google.com/apikey 에서 API 키를 만든다 (구글 계정만 있으면 무료).
2. `.env`의 `GEMINI_API_KEY`와 GitHub 저장소 **Settings → Secrets and variables → Actions**의 `GEMINI_API_KEY`에 넣는다.
3. 시험: `.\td summarize "영상URL" --video-analysis` (자막을 일부러 건너뛰고 영상 분석으로 요약)

키가 없으면 이 기능은 꺼지고, 자막이 막힌 영상은 제목·설명란·썸네일만으로 요약된다.

ntfy를 쓰려면 `config.yaml`에서 `notify.provider: ntfy`로 바꾸고, `.env`의 `NTFY_TOPIC`에 긴 무작위 토픽 이름을 넣는다. 폰의 ntfy 앱에서 그 토픽을 구독하면 된다.

## 3. 기사 사이트 (GitHub Pages)

```powershell
git init -b main
git add .
git commit -m "tube digest"
gh repo create tube-digest --public --source . --push
```

1. GitHub 저장소에서 **Settings → Pages → Source: Deploy from a branch → `main` / `/docs`** 를 선택한다.
2. `config.yaml`을 수정한다.
   - `site.base_url: https://<GitHub아이디>.github.io/tube-digest`
   - `site.git_push: true`

## 4. 영상 하나로 전체 흐름 테스트

```powershell
.\td summarize "https://www.youtube.com/watch?v=영상ID" --publish
```

요약, 기사 생성, push, 배포 대기, 폰 알림까지 한 번에 진행된다. `--publish`를 빼면 로컬 `docs/`에 기사만 만든다.

## 5. 자동 실행 등록 (15분마다)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```

해제하려면 `Unregister-ScheduledTask -TaskName TubeDigest -Confirm:$false`를 실행한다. 로그는 `logs\digest.log`에 남는다.

PC를 켜 두기 어렵다면 `.github/workflows/digest.yml`(옵션 B)을 쓴다. 파일 상단 주석에 준비 사항이 있다.

## 명령어

| 명령 | 설명 |
|---|---|
| `run` | 새 영상 확인 → 요약 → 발행 → 알림 (스케줄러가 호출). `--dry-run`이면 push와 알림 생략 |
| `notify` | 실패했거나 미뤄 둔 알림 재전송 |
| `add` / `remove` / `list` | 채널 관리 |
| `summarize <url> [--publish]` | 특정 영상 즉시 요약 |
| `rebuild` | 템플릿 수정 후 모든 기사 HTML 재생성 (Claude 재호출 없음) |
| `preview` | 샘플 기사로 디자인 확인 (`preview/` 폴더) |
| `test-notify` / `telegram-chat-id` | 알림 설정 확인 |

모든 명령은 `.\td <명령>` 형태로 실행한다. `td.cmd`는 가상환경의 Python으로 `python -m tubedigest`를 대신 실행해 준다.

## 자주 바꾸는 설정

- 요약 지시문: `prompts/summary_prompt.md` (수정하면 다음 요약부터 반영)
- 모델과 비용: `config.yaml`의 `claude.model` (`claude-opus-5-5` 기본, `claude-sonnet-5-5`로 바꾸면 비용 약 절반)
- 쇼츠 포함 여부: `options.skip_shorts`
- 자막 대기 시간: `options.transcript_wait_hours`
