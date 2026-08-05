# -*- coding: utf-8 -*-
"""
COMBO — 이 패키지의 둘째 판. **아직 원고가 없습니다.**

《RULE THE DAY : 데이의 규칙》과 같은 세계(콤보-룰더데이)에 있고,
사건 선택창에서 **잠긴 카드**로 뜹니다. 포스터만 걸려 있고 안은 비어 있습니다.

포스터의 오락실 캐비닛 화면이 「회로기판 도시」인 것이 이 판의 무대입니다 —
룰더데이 쪽은 숲의 굴이었습니다. 같은 캐비닛, 다른 화면.

═══════════════════════════════════════════════════════════════════
 방향 메모 (2026-08-05) — **이 판은 머더미스터리가 아닙니다**
═══════════════════════════════════════════════════════════════════

**2인 협동 방탈출게임**입니다. 「레이튼 교수」식으로 **퍼즐이 본체**이고,
범인을 찾는 판이 아닙니다.

그래서 **지금 이 저장소의 시나리오 틀이 이 판에는 안 맞습니다.**
`template.py` 를 복사해 시작하는 방식 자체가 달라질 수 있습니다.

### 안 쓸 것 (머더미스터리의 뼈대)
- 범인 지목 · 1차 지목 · 알리바이 대화록
- 롤카드의 «감출 것» — 협동판이라 서로 속일 이유가 없습니다
- 「누가 무엇을 아는가」를 갈라 두는 손패 구조
- 진범 · 진상 · 채점

### 유지할 것 (UI 골조는 그대로 갑니다)
- **비주얼노벨** — 오프닝·엔딩·막 사이의 컷
- **퍼즐** — 지금 룰더데이에 붙는 「암호로 여는 카드 → 인벤토리」 구조가 여기서는
  변두리가 아니라 **한복판**이 됩니다
- **공간 탐색** — 구역을 오가며 뒤지는 화면, 해금되는 공간, 인벤토리

즉 **화면은 거의 그대로 쓰고 규칙만 갈아끼우는** 그림입니다.

### 미리 짚어둘 것
- **2인입니다.** 지금 엔진은 「좌석이 다 차야 시작」이라 3인 고정에 맞춰져 있습니다.
  인원이 사건마다 다를 수 있게 열어야 합니다
- 협동판이라 **점수를 서로 다투지 않습니다.** 엔딩이 갈리는 축이 「누가 이겼나」가
  아니라 「어디까지 풀었나」가 됩니다
- 퍼즐이 본체이므로 **정답 판정·힌트 개방·틀린 횟수**가 변두리 기능이 아니라
  제일 많이 손보게 될 자리입니다

═══════════════════════════════════════════════════════════════════

원고를 시작할 때 `META["locked"]` 를 False 로 내리면 선택창에서 열립니다.
그 전까지는 아래 최소 인터페이스가 레지스트리와 서버를 안 터뜨리는 역할만 합니다.
"""

ID = "combo"
TITLE = "COMBO"
SUBTITLE = "COMBO · 준비 중"
META = {
    "title": TITLE, "subtitle": SUBTITLE,
    # 잠긴 카드에서도 이 한 줄은 보입니다. 다음 판이 무엇인지 궁금해질 만큼만 적습니다 —
    # 기믹도 진상도 여기서 말하지 않습니다.
    "blurb": "같은 세계, 다른 판. 아직 열리지 않았습니다.",
    "players": "3인",
    "tone": "준비 중",
    "difficulty": "★★",
    "tagline": "곧 열립니다",
    "locked": True,          # 사건 선택창에서 회색으로 뜨고 고를 수 없습니다
}

DIFFICULTY = "중"
HAND_LIMIT = 1
AP_BY_ROUND: dict = {}

MAP: list = []
COMMON_INTRO = "아직 원고가 없습니다."
VICTIM = ""
SCENE_NOTE = ""
VICTIM_CARD: dict = {}
TRUTH_FULL = ""
CULPRIT_ID = ""
HIDDEN_ID = ""

CHARACTERS: list = []
CARDS: list = []
MEMORY: dict = {}
INTERLUDES: dict = {}
FINAL_QUESTIONS: list = []
ENDINGS: dict = {}
OPENING_CUTS: list = []

# 막이 하나도 없으면 phase_by_seq 가 터집니다. 한 칸만 세워 둡니다.
PHASES = [
    {"seq": 1, "key": "open", "name": "준비 중", "round": 0, "min": 0, "ap": 0,
     "gm": "이 사건은 아직 열리지 않았습니다."},
]


# ── 최소 인터페이스 ──────────────────────────────────────────────
# 데이터가 비어 있어도 레지스트리와 서버가 훑을 때 안 터져야 합니다.
def get_character(cid):
    return next((c for c in CHARACTERS if c["id"] == cid), None)


def get_card(cid):
    return next((c for c in CARDS if c["id"] == cid), None)


def obligatory_cards_upto_round(rnd: int) -> list:
    return []


def public_card(cid: str):
    return None


def private_notes(role_id: str, card_id: str) -> list:
    return []


def phase_by_seq(seq: int) -> dict:
    for p in PHASES:
        if p["seq"] == seq:
            return p
    return PHASES[-1]


def interlude_for(seq: int):
    return None


def memory_up_to(cid: str, current_seq: int, crisis_solved=None) -> list:
    return []


def private_sheet(cid: str):
    return None


def public_scenario() -> dict:
    return {
        "title": TITLE, "subtitle": SUBTITLE, "intro": COMMON_INTRO, "victim": VICTIM,
        "map": MAP, "victimCard": VICTIM_CARD, "sceneNote": SCENE_NOTE, "mapLabel": "",
        "phases": [{"seq": p["seq"], "key": p["key"], "name": p["name"], "min": p["min"],
                    "ap": p["ap"], "gm": p["gm"]} for p in PHASES],
        "characters": [], "npcs": [], "pairKeys": [], "cardCatalog": [],
        "openingCuts": [], "finalQuestions": [], "interludes": {},
    }


def compute_ending(votes, ctx=None):
    return None
