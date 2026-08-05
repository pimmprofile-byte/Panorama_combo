# panorama_combo

**사람 셋이서 하는 온라인 머더미스터리 엔진.**

각자 자기 폰으로 접속해 자기 배역과 자기 정보만 봅니다. 조사한 카드는 손패에
비공개로 들어오고, 알리고 싶으면 대화로 직접 말해야 합니다.

**좌석은 전부 사람입니다.** AI 배역도, LLM 호출도 없습니다 — API 키 없이
돕니다. 그래서 심층심문(AI에게서 답을 끌어내려고 만든 장치)도 없고, 종막
채점도 없습니다. 엔딩은 종막 지목표만으로 갈립니다.

지금 들어 있는 사건은 **《빈 판》 하나**입니다. 돌아가는 걸 확인하고 복사해
가라고 둔 자리표시자이고, 실제 원고는 아직 없습니다.

---

## 바로 돌려보기

```bash
pip install -r requirements.txt
python server.py                      # http://127.0.0.1:8000
```

- `/` 로비 — 사건을 고르고 방을 엽니다
- `/play` 플레이어 화면 (폰 세로 기준)
- `/board` 큰 화면용 진행판
- `/handoff` 진행 인수인계

환경변수는 거의 필요 없습니다. `.env.example` 을 참고하세요 — 포트와
`AGENT_KEY`(진행석 원격 조종용, 비워도 됨)뿐입니다.

---

## 사건 하나 새로 쓰기

```bash
cp scenarios/template.py scenarios/mycase.py
# ID = "mycase" 로 바꾸고
# scenarios/__init__.py 의 임포트 · _MODS · _ORDER 에 추가
```

그게 전부입니다. 로비도 관리자 화면도 `/api/scenarios` 를 읽어 저절로 집어갑니다.

무엇을 채워야 하는지는 **`docs/시나리오_인터페이스.md`** 에 다 있습니다.
필수 항목과, 「적으면 켜지는」 선택 기능(소지품 · 밤 · 질문지 · 갈림길 ·
비상 미니게임 · 구역 배경 그림 …)이 표로 정리돼 있습니다.

`scenarios/template.py` 자체가 그 문서의 실행 가능한 사본입니다 — 이대로도
오프닝부터 진상 공개까지 전 페이즈가 돌아갑니다.

---

## 그림

`assets/` 에 규약대로 파일을 넣으면 화면이 알아서 집어갑니다. 없는 파일은
폴백되므로 **일부만 넣어도 안 깨집니다.**

| 종류 | 규약 |
|---|---|
| 구역 배경 | `{사건id}_room_{art키}.webp` — 16:9 가로 |
| 카드 그림 | `{사건id}_card_{카드ID}.webp` — **3:4 세로** (손에 쥐는 카드 비율) |
| 인물 초상 | `{사건id}_portrait_{배역id}.webp` |
| 오프닝 컷 | `{사건id}_opening_{img키}.webp` |
| 카드 뒷면 | `{사건id}_cardback.webp` |
| 지도 | `{사건id}_map.svg` |
| 포스터 | `poster_{사건id}.jpg` |

구역 배경은 시나리오가 `ROOMS` 를 들고 있어야 찾아갑니다 — 파일만 넣으면
안 뜹니다.

그림을 뽑을 때 쓰는 프롬프트는 `pending/` 에 삽니다. 화풍·규격·리젝 규칙은
`pending/00_화풍_공통.md` 가 정본이고, **서버가 그 마크다운을 그대로 읽어**
관리자 화면의 「에셋 프롬프트」 탭에 뿌립니다. JSON 사본을 안 두는 이유는
문서와 화면이 갈라지지 않게 하려는 것입니다.

효과음은 지금 전부 Web Audio 로 합성합니다(파일 0개). `assets/sfx/` 에 같은
이름의 음원을 넣으면 코드를 안 고치고 그 파일이 대신 울립니다 —
`assets/sfx/README.md` 에 이름표가 있습니다.

---

## 배포 (Render)

`render.yaml` 이 블루프린트입니다. LLM 키가 없어 넣을 값이 사실상 없습니다.

`.github/workflows/render-deploy.yml` 은 `main` 에 올릴 때마다 Render 배포 훅을
직접 찌릅니다. 한 번만 해두면 되는 준비:

1. Render → 서비스 → Settings → **Deploy Hook** 의 URL 복사
2. GitHub → 이 저장소 → Settings → Secrets and variables → Actions
   → New repository secret → 이름 **`RENDER_DEPLOY_HOOK`**, 값은 그 URL

**그 URL은 저장소에 절대 커밋하지 마세요.** 그 자체가 「누구든 이걸 아는 사람은
이 서비스를 배포할 수 있다」는 열쇠입니다.

---

## 구조

```
server.py                  FastAPI. 방 상태는 메모리에 하나. 폴링 1.5초
index.html                 플레이어 화면 (단일 파일)
landing.html               로비 · 관리자
board.html                 큰 화면용 진행판
handoff.py / handoff.html  진행 인수인계
scenarios/
  __init__.py              사건 레지스트리
  template.py              빈 판 — 복사해 가는 틀
docs/시나리오_인터페이스.md  무엇을 채워야 하는가
pending/                   에셋 프롬프트 (서버가 읽어 관리자 화면에 뿌린다)
assets/                    그림 · 폰트 · 효과음
```
