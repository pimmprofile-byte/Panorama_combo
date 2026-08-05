# -*- coding: utf-8 -*-
"""
빈 판 — 사건을 하나 새로 쓸 때 복사해 가는 틀.

이 파일은 «돌아가는 최소한»입니다. 이대로 서버를 켜면 판이 처음부터 끝까지
돌아갑니다 — 배역을 고르고, 오프닝을 보고, 세 구역을 뒤지고, 토론하고,
지목하고, 진상까지 갑니다. 다만 내용이 전부 자리표시자라 재미는 없습니다.

**좌석은 전부 사람입니다.** AI 배역이 없으니 연기 지시도 채점 프롬프트도
안 씁니다. 엔딩은 종막 지목표만으로 갈립니다 — 맨 아래 compute_ending 입니다.

## 쓰는 법

1. 이 파일을 `scenarios/<내사건id>.py` 로 복사합니다.
2. 맨 위 `ID` 를 파일 이름과 같게 바꿉니다. **에셋 파일명이 전부 이 id 로 붙습니다** —
   `assets/{ID}_map.svg`, `assets/{ID}_card_A1.webp`, `assets/{ID}_portrait_{배역id}.webp`.
3. `scenarios/__init__.py` 의 임포트 줄과 `_MODS` · `_ORDER` 에 추가합니다.
4. 위에서부터 아래로 채웁니다. 아래 순서가 실제로 쓰기 편한 순서입니다:
   MAP → CARDS → CHARACTERS → ALIBI_LOG → PHASES → TRUTH_FULL → ENDINGS

## 이 파일에 없는 것 (있으면 엔진이 알아서 켜는 선택 기능)

서버는 없는 속성을 전부 `getattr(SC, "...", 기본값)` 으로 읽습니다. 그래서
**안 쓰는 기능은 그냥 안 적으면 됩니다.** 아래는 적으면 켜지는 것들입니다.

| 속성 | 켜지는 것 |
|---|---|
| `NPCS` · `NPC_LINES` | 고를 수 없는 인물, 그리고 그가 막마다 꺼내는 말 |
| `BELONGINGS` · `BELONGINGS_ORDER` | 소지품 — 1차 지목 최다 득표자가 그 자리에서 압수당한다 |
| `NIGHT_ACTS` | 「밤」 페이즈 — 각자 몰래 하나를 고르고 아침에 결과가 나온다 |
| `ASK_SHEET` | 질문지 페이즈 — 끄덕임/저음만으로 답이 오는 막 |
| `CRISIS` | 판 중간에 끼는 비상 미니게임 (침수·정전 같은 것) |
| `DECISION` · `DESTINATIONS` | 종막 직전의 갈림길 선택 |
| `VAULT_DOCS` | 다 같이 읽는 서류 (조사로 여는 카드가 아니다) |
| `ZONE_LOCK` | 라운드별 구역 잠금 |
| `PHASE_REVEAL` | 그 막이 열리면 저절로 테이블에 오르는 카드 |
| `CARD_PAIRS` / 카드의 `combo` | 두 조각이 맞물리면 저절로 열리는 카드 |
| `ROOMS` | 구역 배경 그림 (`assets/{ID}_room_{art}.webp`) |
| `TURN_ORDER` | 조사 순번 고정 |

전체 목록은 `docs/시나리오_인터페이스.md` 에 있습니다.
"""

# ── 이름표 ────────────────────────────────────────────────────────
ID = "template"                      # 파일 이름 · 에셋 접두사와 같아야 합니다
TITLE = "빈 판"
SUBTITLE = "이 자리에 사건이 들어옵니다"
META = {
    "title": TITLE, "subtitle": SUBTITLE,
    # blurb 은 사건 선택창에서 읽는 글입니다. 기믹을 여기서 말하면 안 됩니다 —
    # 고르는 사람은 아직 아무것도 몰라야 합니다. 1막에서 실제로 겪는 것까지만.
    "blurb": "세 사람이 한자리에 있었고, 하나가 죽은 채로 발견됐다.",
    "players": "3인",
    "tone": "자리표시자",
    "difficulty": "★★",
    "tagline": "복사해서 쓰는 틀",
    # locked=True 로 두면 선택창에서 회색으로 뜨고 못 고릅니다. 원고를 쓰는 동안
    # 켜두면 남이 잘못 눌러 들어오는 일이 없습니다.
    "locked": False,
}

DIFFICULTY = "중"
HAND_LIMIT = 2                       # 손에 남길 수 있는 카드 수. 나머지는 테이블에 깐다
AP_BY_ROUND = {1: 3, 2: 0, 3: 3}     # 라운드별 조사 횟수. 0이면 그 라운드엔 안 뒤진다

PA_LABEL = "안내방송"                 # 막간 방송의 이름표. INTERLUDES 를 안 쓰면 안 보인다
FRAGMENT_LABEL = "떠오른 것"
FRAGMENT_EYEBROW = "기억의 조각"
FRAGMENT_NOTICE = "뭔가 하나 더 떠올랐다 — 아래 「내 정보」를 확인하세요."

# ── 구역 ──────────────────────────────────────────────────────────
# loc 한 글자가 카드의 loc 와 이어집니다. 지도 SVG(assets/{ID}_map.svg)의
# 클릭 영역도 이 글자를 씁니다.
MAP = [
    {"loc": "A", "name": "발견된 자리", "icon": ""},
    {"loc": "B", "name": "그 옆방", "icon": ""},
    {"loc": "C", "name": "바깥", "icon": ""},
]

# 구역 배경 그림을 쓸 때만 적습니다. art 값이 파일명이 됩니다 —
# 아래대로면 assets/template_room_scene.webp 를 찾습니다. 파일이 없으면 그냥 안 뜹니다.
ROOMS = [
    {"key": "A", "loc": "A", "name": "발견된 자리", "art": "scene"},
    {"key": "B", "loc": "B", "name": "그 옆방", "art": "next"},
    {"key": "C", "loc": "C", "name": "바깥", "art": "outside"},
]

# ── 다 같이 읽는 글 ───────────────────────────────────────────────
COMMON_INTRO = (
    "여기에 모두가 같이 읽는 도입부를 적습니다.\n"
    "언제, 어디서, 누가 죽었는지까지만 적습니다 — 왜 죽었는지는 판이 알아냅니다.\n\n"
    "닫힌 자리라는 것을 여기서 못 박아야 합니다. 밖에서 들어온 사람이 있을 수\n"
    "있으면 추리가 성립하지 않습니다."
)
VICTIM = "피해자 이름 · 발견된 자리 · 겉으로 보이는 것 한 줄"
SCENE_NOTE = "발견된 자리. 여기에 첫 화면에 뜰 한 줄을 적습니다."
VICTIM_CARD = {
    "name": "피해자 이름",
    "job": "직함",
    "line": "시신을 처음 봤을 때 눈에 들어오는 것 한 줄.",
}

# 피해자 카드를 막이 지날 때마다 갈아 끼우고 싶을 때만 씁니다. **리스트**입니다 —
# [{"fromSeq": 5, "victim": "...", "sceneNote": "...", "victimCard": {...}}, ...]
# 그 seq 이후로 그 판본이 뜹니다. 안 쓰면 빈 리스트로 둡니다.
VICTIM_STAGES: list = []

# ── 알리바이 대화록 ───────────────────────────────────────────────
# 판이 시작될 때 대화창에 한 줄씩 흘러 들어갑니다(0.5~1.9초 간격, 줄 길이에 따라).
# who 는 배역 id 또는 NPC id 입니다. t 는 그 말을 한 시각(자유 문자열).
ALIBI_HEAD = "각자가 한 말을 그대로 옮긴 것이다 — 참인지는 아무도 모른다."
ALIBI_LOG = [
    {"who": "alpha", "t": "07:00 – 08:00",
     "say": "저는 계속 옆방에 있었습니다. 아무도 안 지나갔어요."},
    {"who": "beta", "t": "07:30쯤",
     "say": "바깥에 나가 있었습니다. 안에서 무슨 소리가 났는지는 모릅니다."},
    {"who": "gamma", "t": "「기억이 안 납니다」",
     "say": "…글쎄요. 그 시간에 뭘 했는지 잘 모르겠는데요."},
    # note 는 말이 아니라 «판이 적는 줄»입니다. 대화록 중간을 끊고 상황을 적습니다.
    # stop: True 를 달면 대화창이 여기서 멈춰 서고 「계속 ›」을 눌러야 이어집니다 —
    # 성격이 다른 대목(증언 → 자기소개)이 이어 붙을 때 씁니다.
    {"note": "그러고 나서 각자 자기소개가 시작되었다.", "stop": True},
    {"who": "alpha", "t": "먼저", "say": "여기에 자기소개를 적습니다."},
    {"who": "beta", "t": "이어서", "say": "짧게, 두 줄 안쪽으로."},
    {"who": "gamma", "t": "마지막", "say": "이 사람만 이 자리에 처음 왔습니다."},
]
ALIBI_NOTE = "각자가 스스로 말한 것을 그대로 옮겼다. 참인지는 아무도 모른다."

# ── 서버에만 있는 정본 ────────────────────────────────────────────
# 플레이어에게는 종막에서만 나갑니다. 채점 프롬프트가 이걸 정답으로 씁니다.
TIMELINE = [
    {"t": "07:00", "who": "", "loc": "A", "text": "여기에 실제로 벌어진 일을 시간순으로 적습니다."},
    {"t": "07:40", "who": "gamma", "loc": "A", "text": "★ 별표를 붙인 줄이 결정적인 자리입니다."},
]
TRUTH_FULL = (
    "여기에 사건의 진상을 통째로 적습니다.\n"
    "누가 왜 무엇을 했는지, 그리고 왜 다른 사람들이 그걸 못 봤는지까지.\n"
    "종막에서 이 글을 다 같이 읽고 판을 닫습니다."
)

# ── 막 ────────────────────────────────────────────────────────────
# key 가 엔진의 화면을 정합니다:
#   open   오프닝(비주얼노벨)      invest 조사턴        talk  토론
#   accuse 1차 지목(비밀투표)      ask    질문지        night 밤
#   final  최후의 선택             decision 갈림길      reveal 진상 공개
# round 는 조사 라운드입니다 — 카드의 round 와 짝을 이룹니다.
# quota 를 적으면 「이 구역들에서 n장」으로 조사를 묶을 수 있습니다.
PHASES = [
    {"seq": 1, "key": "open", "name": "오프닝", "round": 0, "min": 6, "ap": 0,
     "gm": "오프닝을 큰 화면에 띄워 다 같이 봅니다.\n"
           "끝나면 각자 「내 정보」를 읽고, 대화창에서 배역으로 자기소개를 나눕니다."},
    {"seq": 2, "key": "invest", "name": "1차 조사", "round": 1, "min": 14, "ap": 3,
     "gm": "각자 세 자리씩 봅니다. 순서대로 돌아갑니다.\n"
           "★ 다 보고 나면 손에 남길 수 있는 것은 한 장뿐입니다."},
    {"seq": 3, "key": "talk", "name": "1차 토론", "round": 1, "min": 12, "ap": 0,
     "gm": "깔린 카드를 놓고 이야기합니다."},
    {"seq": 4, "key": "accuse", "name": "1차 지목", "round": 1, "min": 5, "ap": 0,
     "gm": "각자 몰래 한 사람의 이름을 적습니다.\n"
           "★ 이 표는 지워지지 않습니다 — 종막까지 그대로 따라갑니다."},
    {"seq": 5, "key": "invest", "name": "마지막 조사", "round": 3, "min": 16, "ap": 3,
     "gm": "각자 세 자리씩 더 봅니다."},
    {"seq": 6, "key": "talk", "name": "2차 토론", "round": 3, "min": 14, "ap": 0,
     "gm": "마지막 토론입니다."},
    {"seq": 7, "key": "final", "name": "최후의 선택", "round": 3, "min": 12, "ap": 0,
     "gm": "각자 질문지에 서술로 답하고, 지목표에 한 사람을 적습니다."},
    {"seq": 8, "key": "reveal", "name": "진상 공개", "round": 3, "min": 12, "ap": 0,
     "gm": "진상과 시각표를 낭독하고 판을 닫습니다."},
]

# 막이 열릴 때 울리는 방송. 키는 seq. 안 쓰면 빈 dict 로 둡니다.
INTERLUDES: dict = {}

CULPRIT_ID = "gamma"                 # 진범의 배역 id. 없는 판이면 "" 로 둡니다
HIDDEN_ID = ""                       # 정체가 따로 있는 배역(있으면)

# ── 배역 ──────────────────────────────────────────────────────────
# 필수: id · name · job · color · tagline · persona · hidden · goals
# 나머지는 있으면 화면이 더 채워지고, 없으면 그 칸이 안 뜹니다.
#
#   hook       롤카드 첫 장. 이 배역이 어떤 처지인지 1인칭으로
#   storyPast  지난 과거 (긴 글 가능)
#   storyToday 오늘 일어난 일 (긴 글 가능)
#   past       [{"w": 시각, "t": 일어난 일}] — 「사건 당일, 나는」 타임라인
#   hide       감출 것. ★ 가 많을수록 치명적이라는 뜻으로 씁니다
#   goals      [{"t": 목표, "p": 점수}] — 이 배역이 오늘 노리는 것
#   sins       이 배역의 죄. 진상 공개에서 읽어줄 몫
#   tips       진행자 참고용. private_sheet 에서 내보낼지는 아래에서 정합니다
#   knows      판 시작부터 아는 카드 id 목록
CHARACTERS = [
    {
        "id": "alpha", "name": "배역 하나", "age": "40", "sex": "여",
        "job": "직함", "avatar": "", "color": "#7A6440",
        "tagline": "한 줄로 이 사람이 누구인지.",
        "known": "다른 사람들이 이 사람에 대해 이미 아는 것.",
        "persona": "말투를 적습니다. 짧게 끊는다 / 존댓말을 안 쓴다 / 화가 나면 조용해진다.",
        "look": "겉모습에서 눈에 걸리는 것 한두 가지.",
        "hidden": False,
        "hook": "1인칭으로 이 배역의 처지를 적습니다.\n왜 여기 있는지, 무엇을 원하는지.",
        "past": [{"w": "07:00", "t": "그 시각에 이 사람이 한 일."}],
        "storyPast": "지난 과거를 적습니다.",
        "storyToday": "오늘 일어난 일을 적습니다.",
        "hide": ["★ 이게 나오면 곤란한 것", "그냥 말하기 싫은 것"],
        "sins": ["진상 공개에서 읽어줄 죄"],
        "goals": [{"t": "★ 가장 큰 목표", "p": 6}, {"t": "두 번째 목표", "p": 3}],
        "tips": ["이 배역을 어떻게 굴리는가 — 처음 하는 사람이 제일 막막해하는 자리."],
        "knows": [],
    },
    {
        "id": "beta", "name": "배역 둘", "age": "35", "sex": "남",
        "job": "직함", "avatar": "", "color": "#43665C",
        "tagline": "한 줄로 이 사람이 누구인지.",
        "known": "다른 사람들이 이 사람에 대해 이미 아는 것.",
        "persona": "말투를 적습니다.",
        "look": "겉모습에서 눈에 걸리는 것.",
        "hidden": False,
        "hook": "1인칭으로 이 배역의 처지를 적습니다.",
        "past": [{"w": "07:30", "t": "그 시각에 이 사람이 한 일."}],
        "storyPast": "지난 과거를 적습니다.",
        "storyToday": "오늘 일어난 일을 적습니다.",
        "hide": ["★ 이게 나오면 곤란한 것"],
        "sins": ["진상 공개에서 읽어줄 죄"],
        "goals": [{"t": "★ 가장 큰 목표", "p": 6}, {"t": "두 번째 목표", "p": 3}],
        "tips": ["이 배역을 어떻게 굴리는가."],
        "knows": [],
    },
    {
        "id": "gamma", "name": "배역 셋", "age": "28", "sex": "남",
        "job": "직함", "avatar": "", "color": "#8A4A3C",
        "tagline": "한 줄로 이 사람이 누구인지.",
        "known": "다른 사람들이 이 사람에 대해 이미 아는 것.",
        "persona": "말투를 적습니다.",
        "look": "겉모습에서 눈에 걸리는 것.",
        "hidden": False,
        "hook": "1인칭으로 이 배역의 처지를 적습니다.",
        "past": [{"w": "07:40", "t": "★ 그 시각에 이 사람이 한 일."}],
        "storyPast": "지난 과거를 적습니다.",
        "storyToday": "오늘 일어난 일을 적습니다.",
        "hide": ["★★ 이게 나오면 그냥 범인이다"],
        "sins": ["사람을 죽인 것"],
        "goals": [{"t": "★ 끝까지 안 걸린다", "p": 8}, {"t": "두 번째 목표", "p": 3}],
        "tips": ["몰릴 때 변명하지 말고 「지금은 말 안 한다」로 버티는 게 덜 몰린다."],
        "knows": [],
    },
]

# 고를 수 없는 인물. 좌석에는 안 들어가고 인물 정보 화면과 대화창에만 나옵니다.
# 안 쓰면 빈 리스트로 두거나 아예 지웁니다.
NPCS = [
    {"id": "delta", "name": "고를 수 없는 사람", "age": "50", "sex": "여",
     "job": "직함", "color": "#5A5A6A",
     "tagline": "한 줄.",
     "known": "다들 아는 것.",
     "look": "겉모습.",
     "note": "이 사람이 그날 무엇을 했는지 — 진행자와 플레이어 모두가 봅니다."},
]

# NPC가 막마다 꺼내는 말. 키는 페이즈 seq. 대화창에 한 줄씩 흘러 들어갑니다.
NPC_LINES = {
    3: [{"who": "delta", "say": "여기에 그 막에서 이 사람이 할 말을 적습니다."}],
}

# ── 조사카드 ──────────────────────────────────────────────────────
# 필수: id · loc · locName · round · title · text
#   spot     구역 안 어느 자리인가 (현황판에 뜨는 이름)
#   hint     이 카드로 무엇을 해야 하는지. 결론은 적지 않습니다
#   reveal   "obligatory" 면 그 라운드에 저절로 전체공개됩니다
#   auto     True 면 뒤져서 못 엽니다 — 판이 때가 되면 엽니다
#   owner    이 배역은 이 카드를 못 엽니다 (자기 자신은 못 뒤진다)
#   requires 먼저 밝혀져야 하는 카드 id
#   combo    [id, id] 두 조각이 모이면 저절로 열립니다
#   gone     이 라운드부터는 그 자리가 사라집니다
#   bait     미끼 카드 표시
#   hot      화면에서 붉게 표시됩니다
CARDS = [
    {"id": "A1", "loc": "A", "locName": "발견된 자리", "round": 1,
     "reveal": "obligatory", "auto": True, "spot": "시신",
     "title": "피해자의 시신",
     "text": "겉으로 보이는 것을 그대로 적습니다.\n판단은 안 적습니다 — 그건 앉은 사람이 합니다.",
     "hint": "여기서부터 시작한다."},
    {"id": "A2", "loc": "A", "locName": "발견된 자리", "round": 1,
     "spot": "바닥", "title": "바닥에 떨어진 것",
     "text": "여기에 카드 본문을 적습니다.",
     "hint": "이게 왜 여기 있는지를 묻는다."},
    {"id": "A3", "loc": "A", "locName": "발견된 자리", "round": 3,
     "spot": "창가", "title": "창가의 자국",
     "text": "3라운드에만 열리는 카드입니다.",
     "hint": "어제와 같은 자리가 아니다."},
    {"id": "B1", "loc": "B", "locName": "그 옆방", "round": 1,
     "spot": "책상", "title": "책상 위",
     "text": "여기에 카드 본문을 적습니다.",
     "hint": "누가 마지막으로 앉았는지."},
    {"id": "B2", "loc": "B", "locName": "그 옆방", "round": 1,
     "owner": "beta", "spot": "가방", "title": "누군가의 가방",
     "text": "owner 를 적으면 그 배역은 이 카드를 못 엽니다.",
     "hint": "주인은 이걸 안 열고 싶어 한다."},
    {"id": "B3", "loc": "B", "locName": "그 옆방", "round": 3,
     "requires": "B1", "spot": "서랍", "title": "잠긴 서랍",
     "text": "B1 이 먼저 밝혀져야 열립니다.",
     "hint": "책상 위가 가리킨 자리."},
    {"id": "C1", "loc": "C", "locName": "바깥", "round": 1,
     "spot": "문 앞", "title": "문 앞의 흔적",
     "text": "여기에 카드 본문을 적습니다.",
     "hint": "드나든 사람이 있었는가."},
    {"id": "C2", "loc": "C", "locName": "바깥", "round": 3,
     "bait": True, "spot": "담장", "title": "담장 너머",
     "text": "미끼 카드입니다. 그럴듯하지만 사건과 상관없습니다.",
     "hint": "이쪽으로 가면 시간을 버린다."},
    {"id": "C3", "loc": "C", "locName": "바깥", "round": 3,
     "combo": ["A2", "C1"], "spot": "맞물린 자리", "title": "두 조각이 맞물린 것",
     "text": "A2 와 C1 이 다 나오면 저절로 열립니다 — 조사턴을 안 씁니다.",
     "hint": "둘을 겹쳐 놓으면 하나가 된다."},
]

# 판이 도는 동안 배역에게 떠오르는 기억. 키는 배역 id, 각 항목의 seq 는 페이즈.
MEMORY = {
    "gamma": [{"seq": 3, "text": "여기에 그 막에서 떠오르는 기억을 적습니다."}],
}

FINAL_QUESTIONS = [
    "누가 그랬다고 생각하는가? 그렇게 생각한 이유는?",
    "당신이 오늘 감춘 것은 무엇인가?",
    "다시 그 시각으로 돌아간다면 무엇을 다르게 하겠는가?",
]

# 종막 엔딩. compute_ending 이 grades 를 보고 이 중 하나를 고릅니다.
ENDINGS = {
    "caught": {"name": "붙잡혔다", "tone": "good",
               "desc": "진범을 맞혔을 때의 마무리 글을 적습니다."},
    "missed": {"name": "놓쳤다", "tone": "bad",
               "desc": "못 맞혔을 때의 마무리 글을 적습니다."},
}

# 오프닝 비주얼노벨. img 는 assets/{ID}_opening_{img}.webp 를 찾습니다.
# fx 는 효과음 키(assets/sfx/README.md 참고). 파일이 없으면 화면만 어둡게 뜹니다.
OPENING_CUTS = [
    {"img": "calm", "fx": "hush",
     "lines": ["여기에 오프닝 첫 장의 대사를 적습니다.", "한 장에 두세 줄이 읽기 좋습니다."]},
    {"img": "body", "fx": "thud",
     "lines": ["그리고 발견됐다."]},
    {"img": "scene", "fx": "",
     "lines": ["밖으로 나가는 길은 하나뿐이었다.", "그러니 이 안에 있는 사람 중 하나가 한 일이다."]},
]



# ══════════════════════════════════════════════════════════════════
#  아래는 엔진이 부르는 인터페이스입니다. 대개 손대지 않아도 됩니다.
# ══════════════════════════════════════════════════════════════════
def get_character(cid: str):
    return next((c for c in CHARACTERS if c["id"] == cid), None)


def get_npc(cid: str):
    return next((n for n in NPCS if n["id"] == cid), None)


def get_card(cid: str):
    return next((c for c in CARDS if c["id"] == cid), None)


def obligatory_cards_upto_round(rnd: int) -> list:
    return [c["id"] for c in CARDS if c.get("reveal") == "obligatory" and c["round"] <= rnd]


def public_card(cid: str):
    """전체공개된 카드로 나가는 몫. 여기 안 적은 필드는 플레이어에게 안 갑니다."""
    c = get_card(cid)
    if not c:
        return None
    return {"id": c["id"], "loc": c["loc"], "locName": c["locName"], "round": c["round"],
            "title": c["title"], "text": c["text"], "bait": c.get("bait", False),
            "spot": c.get("spot", ""), "unlocks": c.get("unlocks", "")}


def private_notes(role_id: str, card_id: str) -> list:
    """그 배역에게만 붙는 카드 메모. 「이건 내가 두고 온 것이다」 같은 것."""
    return []


def phase_by_seq(seq: int) -> dict:
    return next((p for p in PHASES if p["seq"] == seq), PHASES[-1])


def interlude_for(seq: int):
    return INTERLUDES.get(seq)


def memory_up_to(cid: str, current_seq: int, crisis_solved=None) -> list:
    out = []
    for m in MEMORY.get(cid, []):
        if m["seq"] <= current_seq:
            out.append({"when": phase_by_seq(m["seq"])["name"], "text": m["text"]})
    return out


def private_sheet(cid: str):
    """그 배역 본인만 보는 롤카드."""
    c = get_character(cid)
    if not c:
        return None
    return {"id": c["id"], "name": c["name"], "job": c["job"], "avatar": c.get("avatar", ""),
            "color": c["color"], "hidden": c.get("hidden", False), "persona": c["persona"],
            "sex": c.get("sex", ""), "age": c.get("age", ""), "look": c.get("look", ""),
            "goals": c.get("goals", []), "tips": c.get("tips", []),
            "hook": c.get("hook", ""), "storyPast": c.get("storyPast", ""),
            "storyToday": c.get("storyToday", ""), "hide": c.get("hide", []),
            "past": c.get("past", []),
            "knows": [{"id": x["id"], "title": x["title"], "locName": x["locName"],
                       "loc": x["loc"], "spot": x.get("spot", ""), "text": x["text"],
                       "round": x.get("round", 1)}
                      for x in (get_card(k) for k in (c.get("knows") or [])) if x],
            "map": MAP}


def public_scenario() -> dict:
    """모두가 같이 받는 몫. 여기 없는 것은 화면에 아예 안 뜹니다."""
    return {
        "title": TITLE, "subtitle": SUBTITLE, "intro": COMMON_INTRO, "victim": VICTIM,
        "alibiLog": ALIBI_LOG, "alibiNote": ALIBI_NOTE, "alibiHead": ALIBI_HEAD,
        "map": MAP, "victimCard": VICTIM_CARD, "sceneNote": SCENE_NOTE,
        "mapLabel": "이 자리",
        "rooms": [{"key": r["key"], "loc": r["loc"], "name": r["name"], "art": r["art"],
                   "needPod": False} for r in ROOMS],
        # 화면의 이름표. 사건마다 시간대가 달라서 「오늘 밤」이 안 맞는 판이 있습니다.
        "storyLabels": {"past": "지난 과거", "today": "오늘, 일어난 일",
                        "when": "오늘", "goals": "오늘, 내가 노리는 것",
                        "pastTl": "사건 당일, 나는"},
        "victimStages": VICTIM_STAGES,
        "fragLabel": FRAGMENT_LABEL, "fragEyebrow": FRAGMENT_EYEBROW,
        "fragNotice": FRAGMENT_NOTICE,
        "phases": [{"seq": p["seq"], "key": p["key"], "name": p["name"], "min": p["min"],
                    "ap": p["ap"], "gm": p["gm"]} for p in PHASES],
        "characters": [{"id": c["id"], "name": c["name"], "age": c.get("age", ""),
                        "job": c["job"], "avatar": c.get("avatar", ""), "color": c["color"],
                        "tagline": c["tagline"], "look": c.get("look", c["tagline"]),
                        "known": c.get("known", "")} for c in CHARACTERS],
        "npcs": [{"id": n["id"], "name": n["name"], "age": n.get("age", ""), "job": n["job"],
                  "color": n["color"], "tagline": n["tagline"], "look": n.get("look", ""),
                  "known": n.get("known", ""), "note": n.get("note", "")} for n in NPCS],
        "pairKeys": [],
        "cardCatalog": [{"id": c["id"], "loc": c["loc"], "locName": c["locName"],
                         "spot": c.get("spot", ""), "round": c["round"],
                         "auto": bool(c.get("auto")), "hot": bool(c.get("hot")),
                         "gone": c.get("gone", 0),
                         "requires": c.get("requires", "")} for c in CARDS],
        "openingCuts": OPENING_CUTS,
        "finalQuestions": FINAL_QUESTIONS,
        "interludes": {str(k): v for k, v in INTERLUDES.items()},
    }


# ── 엔딩 ─────────────────────────────────────────────────────────
def compute_ending(votes: dict):
    """종막 지목표로 엔딩을 고른다.

    `votes` 는 `{지목한 배역id: 지목당한 배역id}` 다. 사람 셋이 하는 판이라
    채점자가 없다 — 서술 답변은 다 같이 읽는 기록으로 남고, 갈리는 것은 이 표다.

    아직 다 안 적었으면 None 을 돌려주세요. 그러면 화면이 엔딩을 안 엽니다.
    """
    if len(votes) < len(CHARACTERS):
        return None
    tally = {}
    for t in votes.values():
        tally[t] = tally.get(t, 0) + 1
    top = max(tally.values())
    lead = sorted(t for t, v in tally.items() if v == top)
    # 표가 갈리면 아무도 안 잡힌 것으로 본다. 「제일 많이 불린 이름」이 하나여야 검거다.
    caught = (len(lead) == 1 and lead[0] == CULPRIT_ID)
    key = "caught" if caught else "missed"
    e = ENDINGS[key]
    hit = sorted(rid for rid, t in votes.items() if t == CULPRIT_ID)
    return {"key": key, "name": e["name"], "tone": e["tone"], "desc": e["desc"],
            "culprit": CULPRIT_ID, "tally": tally, "lead": lead,
            "accused": hit, "truth": TRUTH_FULL}
