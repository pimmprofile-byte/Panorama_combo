"""
PIMMmurderboard · 졸업사진(卒業寫眞) — 로컬 멀티플레이 게임 서버

각자 자기 PC/폰으로 접속(같은 와이파이). 서버가 '방(room)' 상태를 쥐고 각 기기가 폴링 동기화.
배역의 비밀은 그 배역을 맡은 기기에만. 사람이 안 맡은 배역은 AI가 대본대로 플레이(조사·거짓말·자백).
승리 구조: 오승택을 죽인 범인은 없다 — 종막 질문지(서술형)를 AI가 채점, 모두 자기지목 시 진혼 엔딩.

백엔드 교체형: 기본 Claude API(.env ANTHROPIC_API_KEY) / 무료 Ollama(LLM_BACKEND=ollama, qwen2.5).
실행: pip install -r requirements.txt → cp .env.example .env → python server.py
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import re
import socket
import threading
import time
import urllib.request
import zlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

_HERE = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(_HERE / ".env")
except Exception:
    pass

import handoff  # noqa: E402
import scenarios  # noqa: E402

# 활성 시나리오(앱 전역) — 모든 함수는 전역 SC를 읽으므로, SC를 재바인딩하면 앱 전체가 그 시나리오로 전환된다.
SC = scenarios.get(os.getenv("SCENARIO") or scenarios.default_id())

BACKEND = os.getenv("LLM_BACKEND", "claude").lower()
CLAUDE_MODEL = os.getenv("REUNION_MODEL") or os.getenv("PIMM_MODEL") or "claude-opus-4-8"
# 배역 대사는 한두 문장짜리 응수라 빠른 게 곧 재미다 — 여기만 작은 모델로 돌린다.
# 채점은 판마다 한 번뿐이고 정확해야 하므로 CLAUDE_MODEL을 그대로 쓴다.
CLAUDE_MODEL_FAST = os.getenv("PIMM_MODEL_FAST") or "claude-haiku-4-5-20251001"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "exaone3.5:7.8b")
# 배역 프롬프트는 대본·공개카드·규칙까지 합쳐 5천 토큰이 넘는다. Ollama 기본 컨텍스트는
# 그보다 훨씬 작아서, 안 넘기면 앞쪽(배경·성격·대본)이 조용히 잘린 채로 연기하게 된다 —
# 모델이 못하는 것처럼 보이지만 실은 대본을 못 받은 것이다.
OLLAMA_CTX = int(os.getenv("OLLAMA_CTX", "8192"))
HOST = os.getenv("REUNION_HOST", "0.0.0.0")
# 호스팅(Render 등)은 PORT를 주입 → 그걸 우선 사용, 로컬은 REUNION_PORT/기본값
PORT = int(os.getenv("PORT") or os.getenv("REUNION_PORT", "8790"))
AGENT_KEY = os.getenv("AGENT_KEY", "")  # 에이전트(코드 세션) 원격 조종 키(설정 시 그 키 필요, 미설정 시 개방)
# 심층심문 — 어떤 카드를 증거로 대는지가 추리다. 증거 없이 60%, 엉뚱한 카드면 오히려 20%로
# 떨어지고(허를 찔러 얼버무릴 여지를 준다), 정확한 카드(시나리오의 rebuttal)면 100% 실토.
INTERROGATE_TRUTH_BASE = 0.60
# 같은 곳을 다시 찌를 때마다 붙는 가산. 얼버무림은 막다른 골목이 아니라 한 걸음이어야 한다.
INTERROGATE_PRESS_STEP = 0.30
# 이만큼 찔리면 결국 분다. 심문 횟수가 넉넉하지 않아서, 한 번 얼버무리면 그 다음은 무조건이다.
INTERROGATE_PRESS_BREAK = 1
# 엉뚱한 카드를 들이밀면 오히려 허를 찔러 넘어갈 여지를 준다 — 아무 카드나 대면 분다면
# 「무엇을 들이미는가」가 추리가 아니라 요식이 된다.
INTERROGATE_TRUTH_WRONG_EVIDENCE = 0.25

try:
    import anthropic
except Exception:
    anthropic = None
_ac = None


def _claude(system: str, user: str, mt: int, model: str = "") -> str:
    global _ac
    if anthropic is None:
        raise RuntimeError("anthropic SDK 미설치 — pip install anthropic")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정 (.env) — 또는 LLM_BACKEND=ollama")
    if _ac is None:
        _ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    last = None
    for i in range(3):
        try:
            m = _ac.messages.create(model=model or CLAUDE_MODEL, max_tokens=mt, system=system,
                                    messages=[{"role": "user", "content": user}])
            for b in m.content:
                if getattr(b, "type", None) == "text":
                    return b.text.strip()
            return ""
        except Exception as e:  # noqa: BLE001
            last = e
            if i < 2:
                time.sleep((2 ** i) + random.uniform(0, 0.4))
    raise RuntimeError(f"Claude 호출 실패: {last}")


def _ollama(system: str, user: str, mt: int, model: str = "") -> str:
    payload = {"model": OLLAMA_MODEL, "stream": False,
               "options": {"temperature": 0.85, "num_predict": mt, "num_ctx": OLLAMA_CTX},
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    last = None
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return (json.loads(r.read().decode("utf-8")).get("message", {}).get("content", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < 2:
                time.sleep((2 ** i) + random.uniform(0, 0.4))
    raise RuntimeError(f"Ollama 호출 실패({OLLAMA_URL}, {OLLAMA_MODEL}): {last}")


def llm(system: str, user: str, mt: int = 400, fast: bool = False) -> str:
    """fast=True면 배역 대사용 작은 모델로. Ollama 백엔드는 모델이 하나뿐이라 무시된다."""
    model = CLAUDE_MODEL_FAST if fast else CLAUDE_MODEL
    return _ollama(system, user, mt, model) if BACKEND == "ollama" else _claude(system, user, mt, model)


def backend_ready() -> tuple[bool, str]:
    if BACKEND == "ollama":
        return True, f"Ollama · {OLLAMA_MODEL}"
    if not ANTHROPIC_API_KEY:
        return False, "Claude · API 키 미설정 (.env)"
    if anthropic is None:
        return False, "Claude · anthropic SDK 미설치"
    return True, f"Claude · {CLAUDE_MODEL} (대사 {CLAUDE_MODEL_FAST})"


def _parse_json(raw: str) -> dict:
    try:
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


def current_round(seq: int) -> int:
    # 사건이 페이즈에 라운드를 적어두면 그것을 따른다. 막 수가 사건마다 다른데
    # 여기서 seq 로 잘라 쓰면, 막을 하나 끼워 넣는 순간 조사 라운드가 통째로 밀린다.
    try:
        r = SC.phase_by_seq(seq).get("round")
    except Exception:                                   # noqa: BLE001
        r = None
    if r is not None:
        return int(r)
    if seq >= 6:
        return 3
    if seq >= 4:
        return 2
    if seq >= 2:
        return 1
    return 0


# ── 방 상태 ──
LOCK = threading.RLock()


def fresh_room() -> dict:
    return {
        "rev": 1, "seq": 1,
        "scenarioId": SC.ID,
        # 판마다 새로 생기는 값. 클라이언트가 "이 판에서 오프닝을 봤나"를 이걸로 가른다 —
        # 시나리오 이름으로 기억하면 한 번 본 브라우저에서 영영 안 나온다.
        "roomId": f"r{random.randrange(16**8):08x}",
        "host": None,             # 방 권한자(호스트) clientId — 시나리오 선택·페이즈 진행 통제
        "roles": {c["id"]: {"mode": "open", "clientId": None} for c in SC.CHARACTERS},
        "table": [{"kind": "system", "text": f'— {SC.PHASES[0]["name"]} —'}],
        "revealed": [],           # 전체공개 card id
        "aiActs": [],             # AI가 곧 해야 할 액션(카드 해석·추궁). 침묵 대기 없이 바로 나간다
        "cuts": [],               # 조사 중에 튼 짧은 컷(비주얼노벨). 클라이언트가 안 본 것부터 재생한다
        "hands": {},              # roleId -> [cardId] (손패, 비공개 · 조사/마킹 통합)
        "checkedRound": {},       # roleId -> {cardId: round} (턴별 조사 수 제한 계산용)
        "grades": {},             # roleId -> grade dict (name 포함)
        "finalAnswers": {},       # roleId -> [answer str] (백엔드 미설정 시 진행자 수동채점용 보관)
        "gmSeats": {},            # clientId -> 마지막으로 진행석을 켜둔 시각. 방에 진행자가 있는지 판단용
        "podVotes": {},           # roleId -> 태울 사람. 최종 토론에서 사람이 던진 표만 담는다
        "accuse": {},             # roleId -> 지목한 사람. 종막의 범인 지목, 사람 표만
        # 중간 지목 — 판이 끝나기 전에 한 번 이름을 부르는 사건이 있다. 그 표는 사라지지
        # 않고 종막까지 따라간다. seq 를 같이 적어두는 건 그 막에서만 고칠 수 있게 하려고다.
        "vaultRead": [], "accuse1": {"seq": None, "picks": {}},
        # 1차 지목에서 압수된 소지품의 임자들. 한 번 압수되면 판이 끝날 때까지 펴져 있다.
        "seized": [],
        # 밤 — 각자 몰래 한 가지를 고르고, 그 조합이 그날 밤에 실제로 일어난 일을 정한다.
        "night": {"open": False, "picks": {}, "result": None},
        # 질문지 — 순서대로 하나씩 묻는다. 안 물어진 것이 남는 게 이 막의 요점이다.
        "ask": {"open": False, "asked": [], "turn": None},
        "dest": {},               # roleId -> 향한 곳. 종막에서 각자 정하고, 다 정해야 열린다
        # 결재 결정문 — 자리마다 한 문장씩 고른다. 범인으로 지목된 사람은 결재권을 잃고
        # 그 칸은 남은 사람들의 투표로 찬다. picks 는 최종값, votes 는 잃은 칸의 표다.
        "decision": {"picks": {}, "votes": {}, "extra": ""},
        "itgUsed": [],            # 심층심문에 이미 쓴 카드. 시나리오가 1회성이면 다시 못 쓴다
        "ready": [],              # 「결과 확인」을 누른 배역. 전원이 누르면 판이 다음으로 넘어간다
        "typing": None,
        "events": [],            # 진행 세션이 따라 읽는 사건 기록
        "podOpen": False,        # 특정 카드가 전체공개되면 지도에 탈출 포드가 드러난다
        "podCode": {},           # 배역 -> True. 발사 인증코드를 제 손으로 맞춘 사람들
        "podLaunch": None,       # 발사창이 열린 순간. 코드 없이 탄 사람에게 주는 마지막 10초
        # 침수 대응 퍼즐 — 열림/답안/판정. flood는 0~100, 배치도의 물 높이를 그린다.
        "crisis": {"open": False, "solved": None, "answers": {}},
        "sealed": [],            # 잠긴 구역 — 침수 대응에 실패하면 기관실이 여기 들어간다
        "flood": 0,
        "turn": None,             # 조사 페이즈 현재 차례 roleId (하이브리드 턴)
        "interrogate": {"seq": None, "used": 0, "votes": [], "bonus": False},  # 토론 페이즈 심층심문 예산
        "press": {},              # "배역:카드" -> 얼버무린 횟수. 판이 끝날 때까지 누적된다
        "started": False,         # 호스트가 '이대로 진행'을 확정하면 True — 이후 배역 변경 불가
    }


ROOM = fresh_room()


def use_scenario(sid: str) -> bool:
    """시나리오를 교체하고 방을 새 시나리오로 초기화한다(모두에게 반영)."""
    global SC, ROOM
    if sid not in scenarios.ids():
        return False
    with LOCK:
        prev_host = ROOM.get("host")
        SC = scenarios.get(sid)
        ROOM = fresh_room()
        ROOM["host"] = prev_host   # 호스트는 시나리오 전환 후에도 유지
    return True


def _sync_scenario_state() -> None:
    """방이 쥔 상태를 사건 모듈에 «알려만» 준다.

    밤의 결과에 따라 조사 카드의 글이 달라지는 사건이 있다. 그렇다고 카드 함수마다
    방을 인자로 끌고 다니면 모든 사건이 그 인자를 받아야 한다 — 상태는 여전히 방이
    쥐고, 모듈에는 «지금 방이 이렇다»만 넣어준다. 안 받는 사건에서는 아무 일도 없다.
    """
    fn = getattr(SC, "set_room_state", None)
    if not fn:
        return
    try:
        fn({"night": dict(ROOM.get("night") or {}), "seq": ROOM.get("seq", 1)})
    except Exception:                                   # noqa: BLE001
        pass


def bump():
    ROOM["rev"] += 1
    _sync_scenario_state()


def _ev(kind: str, **fields) -> None:
    """진행 세션이 따라 읽는 사건 기록.

    세션은 푸시를 못 받는다 — 자기 차례가 와야 움직인다. 그래서 서버가 일어난 일을
    번호 붙여 쌓아두고, 세션이 커서 이후만 받아 간다. state를 통째로 비교하는 것보다
    싸고, 무엇이 새로 생겼는지가 분명하다.
    """
    evs = ROOM.setdefault("events", [])
    evs.append({"id": len(evs) + 1, "seq": ROOM["seq"], "kind": kind, **fields})
    del evs[:-400]


def _auto_reveal_obligatory():
    return  # '전체공개' 개념 미사용(우선) — 공개의무 카드도 GM이 대화로 내레이션한다


# ── 침수 대응 퍼즐 ────────────────────────────────────────────────
# 조사 R2에 들어서면 열린다. 배역 과반이 세 문항을 모두 맞히면 물이 멈추고,
# 갈리면 마지막 조사를 물속에서 하게 된다(조사 가능 장수가 준다).
def _crisis_conf():
    return getattr(SC, "CRISIS", None)


def _crisis_public() -> dict | None:
    conf = _crisis_conf()
    if not conf:
        return None
    cr = ROOM.get("crisis") or {}
    if not cr.get("open") and cr.get("solved") is None:
        return None
    pub = SC.crisis_public()
    assigned = [rid for rid, r in ROOM["roles"].items() if r["mode"] in ("human", "ai")]
    pub.update({"open": bool(cr.get("open")), "solved": cr.get("solved"),
                "answered": sorted(cr.get("answers", {}).keys()),
                "total": len(assigned) or len(ROOM["roles"]),
                "need": (len(assigned) or len(ROOM["roles"])) // 2 + 1,
                "outcome": (conf["success"] if cr.get("solved") else conf["fail"]) if cr.get("solved") is not None else "",
                "after": conf.get("after", "") if cr.get("solved") else ""})
    return pub


def _crisis_open():
    """조사 R2 진입 — 사람이 없는 배역은 그 자리에서 자기 답을 낸다."""
    conf = _crisis_conf()
    if not conf:
        return
    cr = ROOM["crisis"]
    if cr.get("open") or cr.get("solved") is not None:
        return
    cr["open"] = True
    cr["answers"] = {}
    for rid, r in ROOM["roles"].items():
        if r["mode"] == "ai":
            cr["answers"][rid] = SC.crisis_ai_answer(rid)
    ROOM["table"].append({"kind": "system", "broadcast": True,
                          "text": f'{conf["title"]} — 각자 화면에서 판단을 고르세요.'})
    _fire_cut("crisis:open")
    _ev("crisis", state="open")
    _crisis_try_resolve()


def _crisis_try_resolve():
    """전원이 답했을 때만 판정한다. GM은 /api/crisis/close 로 앞당길 수 있다."""
    cr = ROOM["crisis"]
    if not cr.get("open"):
        return
    assigned = [rid for rid, r in ROOM["roles"].items() if r["mode"] in ("human", "ai")]
    if assigned and len(cr["answers"]) < len(assigned):
        return
    _crisis_resolve()


def _crisis_resolve():
    conf = _crisis_conf()
    cr = ROOM["crisis"]
    if not conf or not cr.get("open"):
        return
    key = SC.crisis_answer_key()
    assigned = [rid for rid, r in ROOM["roles"].items() if r["mode"] in ("human", "ai")] or list(ROOM["roles"])
    right = sum(1 for rid in assigned if cr["answers"].get(rid) == key)
    ok = right * 2 > len(assigned)
    cr["open"] = False
    cr["solved"] = ok
    if not ok:
        # 해치를 밖에서 잠갔다. 그 안은 그 밤 내내 못 들어간다.
        ROOM["sealed"] = sorted(set((ROOM.get("sealed") or []) + list(conf.get("seals") or [])))
    ROOM["table"].append({"kind": "system", "broadcast": True,
                          "text": conf["success"] if ok else conf["fail"]})
    if ok and conf.get("after"):
        ROOM["table"].append({"kind": "system", "broadcast": True, "text": conf["after"]})
    _fire_cut("crisis:success" if ok else "crisis:fail")
    _ev("crisis", state="solved" if ok else "failed", right=right, of=len(assigned))


def _flood_for(seq: int) -> int:
    """물 높이(0~100). 잡았으면 그 자리에서 멈춘다."""
    cr = ROOM.get("crisis") or {}
    lvl = {1: 0, 2: 6, 3: 18, 4: 32, 5: 46, 6: 60, 7: 74, 8: 84, 9: 90}.get(seq, 0)
    if cr.get("solved") is True:
        return min(lvl, 32)          # 재조정에 성공한 시점에서 굳는다
    return lvl


TABLE_TAIL = 140          # 클라이언트로 내보내는 대화 줄 수. 전체 기록은 서버에만 남는다.


def table_tail(n: int = TABLE_TAIL):
    """대화 기록의 꼬리만, 각 줄에 통 번호를 붙여 내보낸다.

    예전엔 폴링(1.5초)마다 전체 기록을 통째로 보냈다. 80분 세션이면 500줄이 넘고,
    그때쯤엔 한 번 폴 때마다 54KB를 내려받아 다시 파싱하고 화면을 통째로 다시 그렸다.
    번호를 붙여 두면 클라이언트가 새로 온 줄만 덧붙일 수 있다."""
    rows = ROOM["table"]
    tail = rows[-n:] if n and len(rows) > n else rows
    base = len(rows) - len(tail)
    return [dict(m, n=base + i) for i, m in enumerate(tail)]


@contextlib.contextmanager
def _drip():
    """이 블록 안에서 대화창에 붙는 줄은 «한 줄씩» 흘러간다.

    판이 스스로 하는 말은 언제나 여러 줄이 한꺼번에 붙는다 — 알리바이 한 바퀴,
    막이 열릴 때의 GM과 NPC의 말, 밤이 지나고 나오는 것, 압수된 소지품을 놓고
    오가는 말. 그대로 올리면 열다섯 줄이 동시에 솟아서 대화창이 아니라 게시판이
    된다. 누가 누구 말을 받았는지가 거기서 사라진다.

    줄마다 drip 표를 달아두면 클라이언트가 0.5초에 하나씩 푼다. 사람이 친
    말(kind=human)은 여기서 빠진다 — 그건 원래 한 줄씩 오니까 늦출 이유가 없고,
    보낸 사람이 제 말을 못 보고 기다리는 건 더 이상하다.
    """
    start = len(ROOM["table"])
    try:
        yield
    finally:
        _drip_from(start)


def _drip_from(start: int) -> None:
    """`with _drip()` 을 감기엔 블록이 긴 자리용 — 그 번호부터 끝까지에 표를 단다."""
    for m in ROOM["table"][start:]:
        if m.get("kind") != "human":
            m.setdefault("drip", True)


def _my_notes(role_id: str, card_ids) -> dict:
    """그 배역에게만 붙는 카드 메모를 모아 준다. 남의 몫은 애초에 만들지 않는다.

    공개 카드 목록은 모두가 같은 것을 받으므로 여기에 섞을 수 없다 — 별도로 내려보내고
    클라이언트가 카드 위에 얹는다. 배역이 없으면(관전·진행석) 빈 손이다.
    """
    fn = getattr(SC, "private_notes", None)
    if not role_id or not fn:
        return {}
    out = {}
    for cid in card_ids:
        try:
            ns = fn(role_id, cid)
        except Exception:              # noqa: BLE001 — 시나리오가 안 갖췄어도 판은 돌아야 한다
            ns = None
        if ns:
            out[cid] = ns
    return out


def public_state() -> dict:
    with LOCK:
        seq = ROOM["seq"]
        ph = SC.phase_by_seq(seq)
        g = ROOM["grades"]
        ending = SC.compute_ending(g)  # 준비 안 됐으면 시나리오가 None을 반환
        # 진상(정답·범인)은 '진상 공개' 페이즈 전까지 클라이언트로 내보내지 않는다(스포일러 방지).
        if ph.get("key") != "reveal":
            ending = None
        cur = current_round(seq)
        # 화면에 뜨는 장수도 실제로 허용되는 장수여야 한다 — 물을 못 잡았으면 한 장 준다.
        ap = _ap_for(seq)
        # 내용 없는 마킹 현황(누가 어떤 카드를 조사했는지 id만) + 이번 턴 남은 조사 수
        checked = {rid: list(cs) for rid, cs in ROOM["hands"].items() if cs}
        used = {rid: sum(1 for r in cm.values() if r == cur) for rid, cm in ROOM["checkedRound"].items()}
        return {
            "rev": ROOM["rev"], "seq": seq, "round": cur, "scenarioId": SC.ID,
            "roomId": ROOM.get("roomId", ""),
            "podOpen": bool(ROOM.get("podOpen")),
            "podLaunch": _pod_launch_public(),
            "flood": int(ROOM.get("flood", 0)),
            "crisis": _crisis_public(),
            "night": _night_public(),
            "ask": _ask_public(),
            "accuse1": _accuse1_public(),
            "sealed": list(ROOM.get("sealed") or []),
            "chat": {"on": CHAT["on"], "gap": CHAT["gap"]},
            "phase": {"seq": ph["seq"], "key": ph["key"], "name": ph["name"], "gm": ph["gm"], "ap": ap, "min": ph["min"],
                      "noMap": bool(ph.get("noMap")),
                      "quota": [{"label": b.get("label", ""), "locs": list(b.get("locs") or []),
                                 "n": int(b.get("n", 0) or 0)} for b in _quota_for(seq)]},
            "roles": {rid: {"mode": r["mode"], "claimed": r["clientId"] is not None} for rid, r in ROOM["roles"].items()},
            "table": table_tail(),
            "revealed": [SC.public_card(cid) for cid in ROOM["revealed"]],
            "revealedIds": list(ROOM["revealed"]),
            "cuts": list(ROOM.get("cuts") or []),
            "checked": checked,
            "usedAP": used,
            "ready": _ready_state(),
            "itgUsed": (list(ROOM.get("itgUsed") or []) if getattr(SC, "INTERROGATE_ONCE", False) else []),
            "belongLimit": _belong_limit(),
            # 구역 몫을 쓰는 조사 페이즈에서, 배역별로 어느 구역을 몇 장 열었는지.
            "quotaUsed": {rid: _quota_used(rid, cur) for rid in ROOM["roles"]},
            "handLimit": _hand_limit(),
            "pod": _pod_state(),
            # seq 8은 잠수정 기준의 매직넘버였다. 사건마다 막 수가 달라서 페이즈 키로 본다.
            "arrest": (_arrest_state() if ph.get("key") in ("final", "decision", "reveal") else None),
            "dest": (_dest_state() if ph.get("key") in ("final", "decision", "reveal") else None),
            "decision": (_decision_state() if ph.get("key") in ("decision", "reveal") else None),
            # 남의 차례일 때 화면이 멈춘 것처럼 보이지 않게, 다음 차례까지 남은 시간을 내려보낸다.
            "turnWait": (round(max(0.0, AUTO_TURN["delay"] - (time.monotonic() - AUTO_TURN["since"])), 1)
                         if (AUTO_TURN["on"] and ROOM.get("turn")
                             and (ROOM["roles"].get(ROOM["turn"]) or {}).get("mode") == "ai") else None),
            "keepGoals": _keep_goal_results() if ph.get("key") in ("final", "reveal") else [],
            "overLimit": {rid: max(0, len(cs) - _hand_limit()) for rid, cs in ROOM["hands"].items() if len(cs) > _hand_limit()},
            "vault": _vault_public(),
            "turn": ROOM.get("turn") if ap > 0 else None,
            "turnOrder": _turn_order() if ap > 0 else [],
            # 이 라운드에 아직 «누구든» 열 수 있는 자리가 남았는가.
            # 카드보다 턴이 많은 판에서 막바지에 화면이 멈춘 것처럼 보이던 자리다.
            "openLeft": (len(_round_open_pool()) if ap > 0 else None),
            "openIds": (_round_open_pool() if (ap > 0 and DEBUG_POOL) else None),
            "interrogate": _interrogate_budget() if ph.get("key") == "talk" else None,
            "started": bool(ROOM.get("started")),
            "typing": ROOM["typing"],
            "grades": g,
            "ending": ending,
        }


app = FastAPI(title="PIMMmurderboard")

# GM 콘솔(다른 출처의 board.html)이 라이브 서버를 호출할 수 있게 CORS 개방
try:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
except Exception:
    pass

# 이미지 에셋(인물 사진 등)을 /assets 로 서빙 — 파일을 넣는 즉시 UI가 집어간다.
# 규약: assets/{scenarioId}_portrait_{roleId}.png  (없으면 클라이언트가 이모지로 폴백)
try:
    from fastapi.staticfiles import StaticFiles

    class _CachedAssets(StaticFiles):
        """초상·배경·폰트는 한 판 도는 동안 바뀌지 않는다. 캐시 지시를 안 붙이면
        브라우저가 페이지를 넘길 때마다 파일마다 304를 확인하러 다시 다녀온다 —
        폰에서 오프닝을 열 때 컷마다 눈에 띄게 걸리던 원인이다."""

        async def get_response(self, path, scope):
            r = await super().get_response(path, scope)
            r.headers.setdefault("Cache-Control", "public, max-age=3600")
            return r

    _ASSETS = _HERE / "assets"
    _ASSETS.mkdir(exist_ok=True)
    app.mount("/assets", _CachedAssets(directory=str(_ASSETS)), name="assets")
except Exception:
    pass


class Claim(BaseModel):
    roleId: str
    clientId: str


class SetAI(BaseModel):
    roleId: str


class HumanSay(BaseModel):
    roleId: str
    clientId: str
    text: str


class RoleOnly(BaseModel):
    roleId: str


class ChatCtl(BaseModel):
    key: str = ""
    clientId: str = ""
    on: bool | None = None
    gap: float | None = None


class CardOnly(BaseModel):
    cardId: str


class ClientOnly(BaseModel):
    clientId: str


class Investigate(BaseModel):
    cardId: str
    roleId: str
    clientId: str


class RoleReq(BaseModel):
    roleId: str
    clientId: str


class VoteReq(BaseModel):
    roleId: str
    targetRoleId: str
    clientId: str


class PodCode(BaseModel):
    roleId: str
    clientId: str
    code: str


class SwapCard(BaseModel):
    giveId: str      # 내 손패에서 내려놓을 카드
    takeId: str      # 테이블에서 다시 집어올 전체공개 카드
    roleId: str
    clientId: str


class Interrogate(BaseModel):
    askerRoleId: str
    targetRoleId: str
    cardId: str = ""       # 비우면 카드가 아니라 사람을 바로 추궁한다
    evidenceCardId: str = ""
    clientId: str


class InterrogateVote(BaseModel):
    roleId: str
    clientId: str


class AgentSay(BaseModel):
    roleId: str
    text: str
    key: str = ""


class AgentCard(BaseModel):
    cardId: str
    roleId: str = ""
    key: str = ""


class KeyOnly(BaseModel):
    key: str = ""


class SelectScenario(BaseModel):
    scenarioId: str
    key: str = ""
    clientId: str = ""
    force: bool = False        # 진행 중인 방을 일부러 갈아엎을 때만


class CrisisAnswer(BaseModel):
    roleId: str = ""
    clientId: str = ""
    answers: list[int] = []
    key: str = ""


class HostReq(BaseModel):
    clientId: str = ""
    force: bool = False
    # GM 진행석은 호스트와 별개다 — 호스트가 아닌 기기가 진행을 맡을 수 있으므로
    # 그 기기는 키로 자기를 밝힌다. AGENT_KEY를 안 걸어둔 로컬 판에서는 빈 값도 통과한다.
    key: str = ""


class TurnReq(BaseModel):
    clientId: str = ""
    roleId: str = ""
    key: str = ""


class FinalAnswers(BaseModel):
    roleId: str
    clientId: str
    answers: list[str]


@app.get("/api/scenario")
def scenario():
    ok, label = backend_ready()
    d = SC.public_scenario()
    # 클라이언트는 STATE.scenarioId와 이걸 비교해 시나리오가 바뀐 걸 알아챈다.
    # 여태 어느 시나리오도 이 값을 안 실어 보내서, 호스트가 시나리오를 바꿔도
    # 다른 기기는 옛 대본을 그대로 들고 있었다.
    d["scenarioId"] = SC.ID
    d["backend"] = {"ok": ok, "label": label}
    # 조사카드 카탈로그(제목·본문 제외 — 미공개 슬롯 구조만)
    d["cardCatalog"] = [{"id": c["id"], "loc": c["loc"], "locName": c["locName"], "round": c["round"],
                         "spot": c.get("spot", ""),
                         "needs": _card_needs(c),
                         # auto  판이 스스로 여는 자리 · hot  판을 뒤집는 자리
                         # gone  이 라운드부터는 그 자리가 «없다»(어제와 같은 방이 아니다)
                         "auto": bool(c.get("auto")), "hot": bool(c.get("hot")),
                         "gone": c.get("gone", 0),
                         "requires": c.get("requires"), "obligatory": c.get("reveal") == "obligatory"}
                        for c in SC.CARDS]
    return d


@app.get("/api/admin/cards")
def admin_cards(key: str = "", scenarioId: str = ""):
    """검수용 — 한 시나리오의 조사카드를 본문까지 통째로 준다.

    이건 사건의 답을 통째로 내보내는 창구다. 그래서 AGENT_KEY로 잠근다 —
    관리자 비밀번호는 landing.html 안에 그대로 적혀 있어서 잠금이 되지 않는다.
    AGENT_KEY를 안 걸어둔 로컬 판에서는 그냥 열린다.
    """
    if not _agent_ok(key):
        return JSONResponse({"error": "key"}, status_code=403)
    sid = scenarioId or SC.ID
    if sid not in scenarios.ids():
        return JSONResponse({"error": "없는 시나리오"}, status_code=404)
    m = scenarios.get(sid)
    zones = {z["loc"]: z for z in getattr(m, "MAP", [])}
    cards = []
    for c in getattr(m, "CARDS", []):
        z = zones.get(c.get("loc"), {})
        cards.append({
            "id": c.get("id", ""), "loc": c.get("loc", ""),
            "locName": c.get("locName", "") or z.get("name", ""),
            "icon": z.get("icon", ""),
            "spot": c.get("spot", ""), "round": c.get("round", 0),
            "reveal": c.get("reveal", ""), "bait": bool(c.get("bait")),
            "auto": bool(c.get("auto")), "hot": bool(c.get("hot")),
            "gone": c.get("gone", 0), "day2": c.get("day2") or None,
            "requires": c.get("requires", ""), "unlocks": c.get("unlocks", ""),
            "title": c.get("title", ""), "text": c.get("text", ""), "hint": c.get("hint", ""),
        })
    holders = {}
    for ch in getattr(m, "CHARACTERS", []):
        for cid in ch.get("knows", []) or []:
            holders.setdefault(cid, []).append(ch["name"])
    for c in cards:
        c["knownBy"] = holders.get(c["id"], [])
    return {"scenarioId": sid, "title": getattr(m, "TITLE", sid),
            "map": getattr(m, "MAP", []), "cards": cards,
            "pairs": getattr(m, "CARD_PAIRS", []),
            "keepGoals": getattr(m, "KEEP_GOALS", {})}


# ── 에셋 프롬프트 ────────────────────────────────────────────────
# pending/ 밑의 마크다운이 정본이다. 서버가 그걸 그대로 읽어 관리자 화면에 넘긴다.
# JSON을 따로 만들어 두면 문서와 화면이 언젠가 갈라진다 — 그래서 파싱해서 쓴다.
# (사건id, 화면 이름, pending/ 밑 폴더 이름, 탭 색, 이미지 생성 시드)
# 사건을 하나 붙일 때마다 pending/<폴더>/ 에 구역.md · 카드.md 를 두고 여기 한 줄 적는다.
_ASSET_SETS = [
    ("template", "빈 판", "template_빈판", "#c99a4e", 4040),
]
_ASSET_CACHE: dict = {"stamp": None, "data": None}


def _asset_fences(text: str) -> list:
    return re.findall(r"```\n(.*?)```", text, re.S)


def _asset_parse(path: Path) -> list:
    """「### 제목 → `파일명`」 한 덩어리를 항목 하나로 읽는다.

    본문은 인용부호(>)로 적혀 있고, 그 아래 「꼭 보여야 하는 것」과 「리젝」이 붙는다.
    문서를 사람이 읽기 좋게 접어 쓴 줄바꿈은 여기서 편다 — 프롬프트는 한 덩어리로 붙여야 한다.
    """
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").split("\n")
    out, group, i = [], "", 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^#\s+\d+\.\s*(.+)$", line)
        if m:
            group = re.sub(r"\s*—.*$", "", m.group(1)).strip()
        if line.startswith("### "):
            head = line[4:].strip()
            fm = re.search(r"→\s*`([^`]+)`", head)
            if fm:
                cur = {"file": fm.group(1), "group": group,
                       "title": head.split("→")[0].replace("~~", "").strip(),
                       "meta": "", "body": "", "must": "", "reject": ""}
                j, body = i + 1, []
                while j < len(lines) and not lines[j].startswith(("### ", "## ", "# ")):
                    t = lines[j]
                    if t.startswith("> "):
                        body.append(t[2:].rstrip())
                    elif t.startswith(">"):
                        body.append(t[1:].rstrip())
                    elif t.startswith("**꼭 보여야 하는 것:**"):
                        cur["must"] = t.split("**", 2)[2].strip()
                    elif t.startswith("**리젝:**"):
                        cur["reject"] = t.split("**", 2)[2].strip()
                    elif t.startswith("**") and not cur["meta"] and not body:
                        cur["meta"] = t.replace("**", "").strip()
                    j += 1
                cur["body"] = re.sub(r"\s+", " ", " ".join(x.strip() for x in body if x.strip())).strip()
                if cur["body"]:
                    out.append(cur)
                i = j
                continue
        i += 1
    return out


def _asset_prompts() -> dict:
    """pending/ 을 통째로 읽어 화면이 쓸 모양으로 준다. 파일이 안 바뀌면 다시 안 읽는다."""
    root = _HERE / "pending"
    files = sorted(root.rglob("*.md")) if root.exists() else []
    stamp = tuple((str(p), p.stat().st_mtime_ns) for p in files)
    if _ASSET_CACHE["stamp"] == stamp and _ASSET_CACHE["data"]:
        return _ASSET_CACHE["data"]

    common = {"zone": "", "card": "", "ref": ""}
    cp = root / "00_화풍_공통.md"
    if cp.exists():
        f = _asset_fences(cp.read_text(encoding="utf-8"))
        for k, idx in (("zone", 0), ("card", 1), ("ref", 2)):
            if len(f) > idx:
                common[k] = f[idx].strip()

    scen = []
    for sid, name, folder, hue, seed in _ASSET_SETS:
        d = root / folder
        zp, kp = d / "구역.md", d / "카드.md"
        tone = ""
        for src in (zp, kp):
            if not src.exists():
                continue
            for f in _asset_fences(src.read_text(encoding="utf-8")):
                if "The place" in f:
                    tone = f.strip()
                    break
            if tone:
                break
        zones, cards = _asset_parse(zp), _asset_parse(kp)
        for it in zones + cards:
            it["have"] = (_HERE / "assets" / it["file"]).exists()
        scen.append({"id": sid, "name": name, "hue": hue, "seed": seed,
                     "tone": tone, "zones": zones, "cards": cards})

    data = {"common": common, "scen": scen}
    _ASSET_CACHE["stamp"], _ASSET_CACHE["data"] = stamp, data
    return data


@app.get("/api/admin/assets")
def admin_assets(key: str = ""):
    """검수용 — pending/ 의 에셋 프롬프트를 관리자 화면에 그대로 넘긴다.

    프롬프트 본문에는 「무엇이 보여야 하는가」가 적혀 있고, 그게 곧 그 카드의 답이다.
    그래서 카드·배역 훑어보기와 똑같이 AGENT_KEY로 잠근다.
    """
    if not _agent_ok(key):
        return JSONResponse({"error": "key"}, status_code=403)
    return _asset_prompts()


@app.get("/api/admin/roles")
def admin_roles(key: str = "", scenarioId: str = ""):
    """검수용 — 한 시나리오의 배역을 롤카드 통째로 준다.

    카드 훑어보기(/api/admin/cards)와 짝이다. 카드만 봐서는 그 카드가 누구의
    무기이고 누구의 약점인지가 안 보인다. 여기서 목표·비밀·심문 대사까지 같이 펼쳐야
    배분이 맞는지 판단이 선다. 범인이 누구인지도 그대로 나온다 —
    그래서 카드 쪽과 똑같이 AGENT_KEY로 잠근다.
    """
    if not _agent_ok(key):
        return JSONResponse({"error": "key"}, status_code=403)
    sid = scenarioId or SC.ID
    if sid not in scenarios.ids():
        return JSONResponse({"error": "없는 시나리오"}, status_code=404)
    m = scenarios.get(sid)
    culprit = getattr(m, "CULPRIT_ID", "")
    hidden = getattr(m, "HIDDEN_ID", "")
    keep = getattr(m, "KEEP_GOALS", {}) or {}
    memory = getattr(m, "MEMORY", {}) or {}
    interro = getattr(m, "INTERROGATE", {}) or {}
    plain = getattr(m, "INTERROGATE_PLAIN", {}) or {}
    frag_key = getattr(m, "TALK_FRAGMENT_KEY", {}) or {}
    phases = {p["seq"]: p["name"] for p in getattr(m, "PHASES", [])}
    titles = {c.get("id"): c.get("title", "") for c in getattr(m, "CARDS", [])}
    spots = {c.get("id"): (f'{c.get("locName","")} · {c.get("spot","")}'
                           if c.get("spot") else c.get("locName", ""))
             for c in getattr(m, "CARDS", [])}

    def card_ref(cid: str) -> dict:
        return {"id": cid, "title": titles.get(cid, ""), "where": spots.get(cid, "")}

    roles = []
    for ch in getattr(m, "CHARACTERS", []):
        cid = ch["id"]
        kg = keep.get(cid)
        # 기억의 파편은 t1/t2/t3로 들어 있고 어느 페이즈에 뜨는지는 따로 적혀 있다.
        # 검수할 때 «몇 번째 토론에서 나오는 말인가»가 붙어 있어야 순서를 볼 수 있다.
        frags = []
        for seq in sorted(frag_key):
            k = frag_key[seq]
            txt = (memory.get(cid) or {}).get(k)
            if txt:
                frags.append({"seq": seq, "key": k, "phase": phases.get(seq, ""), "text": txt})
        ig = []
        for card_id, e in (interro.get(cid) or {}).items():
            ig.append({"card": card_ref(card_id), "truth": e.get("truth", ""),
                       "evasive": e.get("evasive", ""),
                       "rebuttal": card_ref(e["rebuttal"]) if e.get("rebuttal") else None})
        roles.append({
            "id": cid, "name": ch.get("name", ""), "age": ch.get("age", ""),
            "job": ch.get("job", ""), "avatar": ch.get("avatar", ""), "color": ch.get("color", ""),
            "tagline": ch.get("tagline", ""), "look": ch.get("look", ""),
            "persona": ch.get("persona", ""),
            "isCulprit": cid == culprit, "isHidden": cid == hidden or bool(ch.get("hidden")),
            "past": ch.get("past", []) or [],
            "hook": ch.get("hook", ""), "storyPast": ch.get("storyPast", ""), "tips": ch.get("tips", []) or [],
            "storyToday": ch.get("storyToday", ""),
            "hide": ch.get("hide", []) or [],
            "sins": ch.get("sins", []) or [],
            "goals": ch.get("goals", []) or [],
            "keepGoal": ({"label": kg.get("label", ""), "points": kg.get("points", 0),
                          "fail": kg.get("fail", ""),
                          "cards": [card_ref(x) for x in kg.get("cards", [])]} if kg else None),
            "knows": [card_ref(x) for x in (ch.get("knows") or [])],
            "fragments": frags,
            "aiNote": ch.get("ai_note", ""),
            "interrogate": ig,
            "interrogatePlain": plain.get(cid, ""),
        })
    return {"scenarioId": sid, "title": getattr(m, "TITLE", sid),
            "fragLabel": getattr(m, "FRAGMENT_LABEL", "") or "기억의 파편",
            "roles": roles}


@app.get("/api/scenarios")
def scenarios_list():
    return {"scenarios": scenarios.meta_list(), "active": SC.ID}


@app.post("/api/select")
def select_scenario(b: SelectScenario):
    if b.scenarioId not in scenarios.ids():
        return JSONResponse({"error": "없는 시나리오"}, status_code=400)
    if getattr(scenarios.get(b.scenarioId), "META", {}).get("locked"):
        return JSONResponse({"error": "아직 준비 중인 사건입니다"}, status_code=400)
    with LOCK:
        same = (b.scenarioId == SC.ID)
        running = bool(ROOM.get("started"))
        held = ROOM.get("host")
        # 판이 안 돌고 있으면 지킬 게 없다. 예전엔 여기서도 호스트를 요구해서,
        # 앞선 세션의 브라우저가 호스트를 쥔 채 사라지면 아무도 사건을 못 바꿨다 —
        # 무엇을 골라도 그 방에 마지막으로 올라와 있던 사건으로 들어가버렸다.
        if not running and held is None and b.clientId:
            ROOM["host"] = b.clientId
            held = b.clientId
    mine = (held in (None, b.clientId)) or _gm_key_ok(b.key)
    # 같은 사건을 다시 고른 것뿐이면 방을 건드리지 않는다. 예전엔 이것도 초기화라
    # 호스트가 뒤로 가기로 로비에 들렀다 돌아오기만 해도 판이 통째로 날아갔다.
    if same:
        return {"ok": True, "active": SC.ID, "unchanged": True, "started": running}
    if running:
        # 진행 중인 방은 사건을 못 바꾼다. 바꾸면 배역·손패·공개카드가 전부 사라진다 —
        # 다른 기기에서 링크를 다시 연 사람에게 그 권한이 있어선 안 된다.
        if not b.force:
            return JSONResponse({"error": f"《{SC.TITLE}》 판이 진행 중입니다",
                                 "active": SC.ID, "activeTitle": SC.TITLE, "started": True},
                                status_code=409)
        if not mine:
            return JSONResponse({"error": "host", "active": SC.ID, "activeTitle": SC.TITLE},
                                status_code=403)
    elif not mine:
        return JSONResponse({"error": "host", "active": SC.ID, "activeTitle": SC.TITLE},
                            status_code=403)
    use_scenario(b.scenarioId)  # 방을 새 시나리오로 초기화(호스트는 유지)
    return {"ok": True, "active": SC.ID}


@app.get("/api/state")
def state(clientId: str = "", gm: int = 0):
    st = public_state()
    with LOCK:
        # 진행석은 각자 기기에서 토글하는 것이라 서버는 여태 그 존재를 몰랐다.
        # 그래서 다른 기기에 앉은 사람은 «진행석이 없다»고 판단해 버렸다 — 종막 답안이 갈 곳을
        # 정하는 갈림길이 그 판단에 걸려 있다. 폴링에 얹어 자리를 알리게 하고, 끊기면 저절로 비운다.
        now = time.time()
        if clientId:
            if gm:
                ROOM["gmSeats"][clientId] = now
            else:
                ROOM["gmSeats"].pop(clientId, None)
        for cid, t in list(ROOM["gmSeats"].items()):
            if now - t > 20:           # 폴링이 1.5초 간격이니 20초면 확실히 떠난 것이다
                ROOM["gmSeats"].pop(cid, None)
        st["hasGM"] = bool(ROOM["gmSeats"])
        # 「추가 정보」가 몇 장인가. 화면은 이 숫자가 늘어난 것만 보고 알림을 띄운다 —
        # 무엇이 늘었는지는 열어봐야 안다.
        mine0 = next((rid for rid, r in ROOM["roles"].items() if r["clientId"] and r["clientId"] == clientId), "")
        if mine0:
            n = 0
            try:
                n += len(SC.memory_up_to(mine0, ROOM["seq"]))
            except Exception:                                # noqa: BLE001
                pass
            if ((ROOM.get("ask") or {}).get("asked") or []):
                n += 1
            if ((ROOM.get("night") or {}).get("result")):
                n += 1
            st["extraN"] = n
        # 자기가 이미 적었는지는 자기만 안다. 남이 무엇을 적었는지는 다 던진 뒤에 열린다.
        if st.get("accuse1") is not None:
            who = next((rid for rid, r in ROOM["roles"].items() if r["clientId"] and r["clientId"] == clientId), "")
            # 내가 무엇을 적었는지는 언제든 나만 볼 수 있다. 남의 표는 종막까지 안 열린다.
            st["accuse1"]["mineDone"] = who in ((ROOM.get("accuse1") or {}).get("picks") or {})
            st["accuse1"]["mine"] = ((ROOM.get("accuse1") or {}).get("picks") or {}).get(who, "")
        # 소지품 — 내 것과, 압수돼 펴진 것만. 남의 주머니는 압수 전에는 안 보인다.
        _who = next((rid for rid, r in ROOM["roles"].items() if r["clientId"] and r["clientId"] == clientId), "")
        _bl = _belongings_public(_who)
        if _bl:
            st["belongings"] = _bl
        # 밤의 선택지는 그 사람 것만 내려간다. 공개 상태에는 «누가 정했나»만 있다.
        if st.get("night") is not None:
            mine = next((rid for rid, r in ROOM["roles"].items() if r["clientId"] and r["clientId"] == clientId), "")
            st["night"] = _night_public(mine)
            # 그 사람의 밤은 그 사람 화면에서만 돈다. 방이 다 같이 보는 컷 목록에는
            # 못 넣는다 — 넣는 순간 누가 무엇을 했는지가 통째로 새어 나간다.
            mc = (st["night"] or {}).get("mineCuts") or []
            if mc:
                st["cuts"] = [{"id": f"night:mine:{mine}", "cuts": mc}] + list(st.get("cuts") or [])
        # 종막에는 답안을 모두가 볼 수 있어야 한다. 진행석이 없으면 누구든 한 덩어리로 묶어
        # 클로드에 물어보러 가야 하는데, 여태 그 답안은 AGENT_KEY로 잠긴 /api/gm에만 있었다.
        # 각자 자기 것만 들고 있으면 판이 흩어진 방에서는 아무도 전체를 못 만든다.
        if (st.get("phase") or {}).get("key") == "final":
            st["finalAnswers"] = dict(ROOM["finalAnswers"])
        # 진상 공개에서는 그날 밤의 시각표와 진상 전문을 푼다. 알리바이가 이 장르의 뼈대라
        # 마지막에 분 단위로 맞춰 보여줘야 «아, 그래서 그랬구나»가 온다.
        # 채점이 붙든 안 붙든 이 화면은 비면 안 된다 — 진행자 없이 도는 방이 대부분이다.
        if (st.get("phase") or {}).get("key") == "reveal":
            st["timeline"] = list(getattr(SC, "TIMELINE", []) or [])
            st["truthFull"] = getattr(SC, "TRUTH_FULL", "")
            st["culpritId"] = getattr(SC, "CULPRIT_ID", "")
            # 시각표를 선으로 그리려면 구역 이름이 있어야 한다. 방 목록에서 그대로 끌어온다.
            # 시각표에 loc 이 없는 시나리오는 선 그림을 건너뛰고 표만 그린다.
            # 방 상세(ROOMS)가 없는 시나리오는 배치도(MAP)에서 끌어온다 — 어느 쪽이든 구역 이름은 있다.
            locs, seen = [], set()
            for r in (getattr(SC, "ROOMS", None) or getattr(SC, "MAP", []) or []):
                k = r.get("key") or r.get("loc")
                if k and k not in seen:
                    seen.add(k)
                    locs.append({"id": k, "name": r.get("name") or k})
            st["timelineLocs"] = locs
        st["hasHost"] = ROOM.get("host") is not None
        # 내가 맡은 배역. 예전엔 클라이언트가 localStorage 기억만 보고 판단해서,
        # 잡은 직후나 새로고침 뒤에 자기 배역을 '참여 중'(남이 맡음)으로 그리곤 했다.
        st["myRole"] = next((rid for rid, r in ROOM["roles"].items()
                             if clientId and r["clientId"] == clientId), None)
        if st["myRole"]:
            # 내가 던진 표만 나에게 돌려준다. 남의 표는 열릴 때까지 아무에게도 안 간다.
            if st.get("pod") is not None:
                st["pod"]["mine"] = ROOM["podVotes"].get(st["myRole"])
            st["myAccuse"] = ROOM["accuse"].get(st["myRole"])
            st["myDest"] = ROOM.get("dest", {}).get(st["myRole"])
            _d = ROOM.get("decision") or {}
            st["myDecision"] = (_d.get("picks") or {}).get(st["myRole"])
            st["overBelong"] = _over_belong(st["myRole"])
            st["impression"] = dict((getattr(SC, "IMPRESSION", {}) or {}).get(st["myRole"], {}))
            st["myDecisionVotes"] = {seat: v.get(st["myRole"])
                                     for seat, v in (_d.get("votes") or {}).items()
                                     if v.get(st["myRole"])}
        # 내가 볼 수 있는 카드(전체공개 + 내 손패)에 대해서만, 나에게만 붙는 메모를 얹는다.
        if st["myRole"]:
            seen = list(ROOM["revealed"]) + list(ROOM["hands"].get(st["myRole"], []))
            st["myNotes"] = _my_notes(st["myRole"], seen)
            if st.get("vault"):
                st["vault"] = dict(st["vault"], mine=st["myRole"] in (ROOM.get("vaultRead") or []))
        # 포드 — 내 지도에 자리가 찍혔는가, 그리고 내가 코드를 맞췄는가.
        # 남이 맞췄는지는 내보내지 않는다. 그게 새면 표가 그리로만 쏠린다.
        if st.get("myRole"):
            st["podMarked"] = _pod_marked(st["myRole"])
            st["podCodeOk"] = bool(ROOM["podCode"].get(st["myRole"]))
        st["isHost"] = bool(clientId) and ROOM.get("host") == clientId
        if st["isHost"]:
            ROOM["hostSeen"] = time.time()
        # 호스트를 쥔 기기가 사라지면(창을 닫았거나, 저장소를 지웠거나, 다른 폰으로 옮겼거나)
        # 아무도 판을 못 굴린다. 그 자리는 잠깐 비면 남이 이어받을 수 있어야 한다.
        st["hostStale"] = bool(ROOM.get("host")) and not st["isHost"] and _host_stale()
        # 호스트를 아무도 안 잡은 방도 있다. 그때는 '호스트 전용' 연출을 아무도 못 보게 되므로
        # 클라이언트가 그 사정을 알 수 있게 해준다(다른 엔드포인트도 같은 규칙으로 통과시킨다).
        st["hasHost"] = ROOM.get("host") is not None
        # 내가 맡은 배역. 예전엔 클라이언트가 localStorage 기억만 보고 판단해서,
        # 잡은 직후나 새로고침 뒤에 자기 배역을 '참여 중'(남이 맡음)으로 그리곤 했다.
        st["myRole"] = next((rid for rid, r in ROOM["roles"].items()
                             if clientId and r["clientId"] == clientId), None)
    return st


def _seed_alibi() -> None:
    """알리바이 한 바퀴를 대화창에 깔아둔다.

    예전에는 오프닝 화면 옆의 접힌 패널이었다. 거기 두면 조사 페이즈로 넘어가는 순간
    화면에서 사라져서, 정작 대조가 필요한 토론 때 아무도 다시 못 봤다. 대화 기록으로
    남겨두면 위로 올려 언제든 다시 읽을 수 있고, AI 배역도 같은 것을 읽는다.
    """
    log = getattr(SC, "ALIBI_LOG", None) or []
    if not log:
        return
    head = getattr(SC, "ALIBI_HEAD", "") or "각자가 한 말을 그대로 옮긴 것이다 — 참인지는 아무도 모른다."
    PRE = "사건 당시 · 알리바이 대화록 — "
    # 한 사람씩 증언하는 자리다. 열세 줄이 동시에 솟으면 «대화록»이 아니라 벽보가 된다.
    with _drip():
        ROOM["table"].append({"kind": "system", "broadcast": True, "text": PRE + head})
        for a in log:
            # 말이 아니라 «판이 적는 줄». 대화록 중간에 한 번 끊고 무슨 일이 벌어졌는지 적는다.
            if a.get("note"):
                row = {"kind": "system", "broadcast": True, "text": PRE + a["note"]}
                # 「여기서 한 번 끊는다」 — 대화창이 여기서 멈춰 서고 「계속」을 기다린다
                if a.get("stop"):
                    row["stop"] = True
                ROOM["table"].append(row)
                continue
            # 배역이 아닌 사람도 이 자리에 선다 — 마부도 왕진의도 그날 아침 어디 있었는지를 말한다.
            who = a.get("who", "")
            getnpc = getattr(SC, "get_npc", None)
            c = SC.get_character(who) or (getnpc(who) if getnpc else None) or {}
            ROOM["table"].append({"kind": "alibi", "roleId": who,
                                  "speaker": c.get("name", who),
                                  "at": a.get("t", ""), "text": a.get("line") or a.get("say", "")})
        note = getattr(SC, "ALIBI_NOTE", "")
        if note:
            ROOM["table"].append({"kind": "system", "text": note})


@app.post("/api/start")
def start_game(b: HostReq):
    """호스트가 배역 확정 — 이후 배역은 바꿀 수 없고, 모두가 오프닝으로 들어간다."""
    with LOCK:
        if ROOM.get("host") is not None and not (_is_host(b.clientId) or _agent_ok(b.key)):
            return JSONResponse({"error": "호스트만 시작할 수 있습니다"}, status_code=403)
        opens = [rid for rid, r in ROOM["roles"].items() if r["mode"] == "open"]
        if opens:
            return JSONResponse({"error": f"아직 정해지지 않은 배역이 {len(opens)}개 있습니다"}, status_code=409)
        ROOM["started"] = True
        with _drip():
            ROOM["table"].append({"kind": "system", "broadcast": True,
                                  "text": "배역이 확정됐습니다. 오프닝을 시작합니다."})
            _ph0 = SC.phase_by_seq(ROOM["seq"])
            if _ph0.get("gm"):
                ROOM["table"].append({"kind": "gm", "broadcast": True, "text": _ph0["gm"]})
            _seed_alibi()
        bump()
    return {"ok": True, "started": True}


def _roles_locked() -> bool:
    return bool(ROOM.get("started"))


# 호스트가 이만큼 조용하면 자리를 비운 것으로 본다. 판 중에 한참 관망하는 사람이 있어서
# 짧게 잡으면 멀쩡히 보고 있는 호스트를 빼앗게 된다.
HOST_STALE_SEC = 300


def _host_stale() -> bool:
    seen = ROOM.get("hostSeen")
    return seen is None or (time.time() - seen) > HOST_STALE_SEC


@app.post("/api/host/claim")
def host_claim(b: HostReq):
    with LOCK:
        if not b.clientId:
            return JSONResponse({"error": "clientId"}, status_code=400)
        # 비어 있거나 내 것이면 그냥 잡는다. 남이 쥐고 있어도 그쪽이 한참 조용하거나
        # 이쪽이 작정하고 가져가겠다고 하면 넘겨준다 — 친구들끼리 도는 방이고,
        # 여기서 막아봐야 판이 멈추는 것 말고는 지켜지는 게 없다.
        if ROOM.get("host") in (None, b.clientId):
            ROOM["host"] = b.clientId
            ROOM["hostSeen"] = time.time()
            bump()
            return {"ok": True, "isHost": True}
        if b.force or _host_stale():
            prev = ROOM.get("host")
            ROOM["host"] = b.clientId
            ROOM["hostSeen"] = time.time()
            ROOM["table"].append({"kind": "system", "broadcast": True,
                                  "text": "진행 권한이 다른 기기로 넘어갔습니다."})
            bump()
            return {"ok": True, "isHost": True, "tookOver": True, "prev": bool(prev)}
        return {"ok": False, "isHost": False, "hasHost": True, "stale": False}


@app.post("/api/host/release")
def host_release(b: HostReq):
    with LOCK:
        if ROOM.get("host") == b.clientId:
            ROOM["host"] = None
            bump()
        return {"ok": True}


@app.post("/api/claim")
def claim(b: Claim):
    with LOCK:
        if _roles_locked():
            return JSONResponse({"error": "게임이 시작돼 배역을 바꿀 수 없습니다"}, status_code=409)
        r = ROOM["roles"].get(b.roleId)
        if not r:
            return JSONResponse({"error": "없는 배역"}, status_code=404)
        if r["clientId"] and r["clientId"] != b.clientId:
            return JSONResponse({"error": "이미 다른 사람이 맡은 배역입니다"}, status_code=409)
        for rr in ROOM["roles"].values():
            if rr["clientId"] == b.clientId:
                rr["clientId"] = None
                rr["mode"] = "open"
        r["clientId"] = b.clientId
        r["mode"] = "human"
        bump()
    return {"ok": True}


@app.post("/api/claim-random")
def claim_random(b: ClientOnly):
    with LOCK:
        if _roles_locked():
            return JSONResponse({"error": "게임이 시작돼 배역을 바꿀 수 없습니다"}, status_code=409)
        for rid, r in ROOM["roles"].items():
            if r["clientId"] == b.clientId:
                return {"ok": True, "roleId": rid}
        opens = [rid for rid, r in ROOM["roles"].items() if r["mode"] == "open"]
        if not opens:
            return JSONResponse({"error": "빈 배역이 없습니다"}, status_code=409)
        rid = random.choice(opens)
        ROOM["roles"][rid]["clientId"] = b.clientId
        ROOM["roles"][rid]["mode"] = "human"
        bump()
    return {"ok": True, "roleId": rid}


@app.post("/api/release")
def release(b: Claim):
    with LOCK:
        if _roles_locked():
            return JSONResponse({"error": "게임이 시작돼 배역을 바꿀 수 없습니다"}, status_code=409)
        r = ROOM["roles"].get(b.roleId)
        if r and r["clientId"] == b.clientId:
            r["clientId"] = None
            r["mode"] = "open"
            bump()
    return {"ok": True}


@app.post("/api/setai")
def setai(b: SetAI):
    with LOCK:
        if _roles_locked():
            return JSONResponse({"error": "게임이 시작돼 배역을 바꿀 수 없습니다"}, status_code=409)
        r = ROOM["roles"].get(b.roleId)
        if not r:
            return JSONResponse({"error": "없는 배역"}, status_code=404)
        if r["mode"] == "human":
            return JSONResponse({"error": "사람이 맡은 배역"}, status_code=409)
        r["mode"] = "ai" if r["mode"] != "ai" else "open"
        r["clientId"] = None
        bump()
    return {"ok": True}


@app.get("/api/sheet/{role_id}")
def sheet(role_id: str, clientId: str = ""):
    with LOCK:
        r = ROOM["roles"].get(role_id)
        seq = ROOM["seq"]
        if not r:
            return JSONResponse({"error": "없는 배역"}, status_code=404)
        if r["clientId"] != clientId:  # 엄격: 내가 '맡은' 배역만 (빈자리·AI 배역 비밀 열람 차단)
            return JSONResponse({"error": "자기 배역만 열람할 수 있습니다"}, status_code=403)
    s = SC.private_sheet(role_id)
    _cs = (ROOM.get("crisis") or {}).get("solved")
    try:
        s["fragments"] = SC.memory_up_to(role_id, seq, _cs)
    except TypeError:                      # 위기 개념이 없는 시나리오
        s["fragments"] = SC.memory_up_to(role_id, seq)
    # 그날 밤은 줄글 조각이 아니라 제 자리를 갖는다 — 시트가 한 장으로 세운다.
    nr = getattr(SC, "night_report", None)
    if nr:
        try:
            s["nightReport"] = nr(role_id, ROOM.get("night") or {})
        except Exception:                  # noqa: BLE001
            s["nightReport"] = None
    # 그날 밤 자기가 한 일은 미리 적어둘 수가 없다 — 고른 다음에야 생긴다.
    for hook, key in ((getattr(SC, "night_memory", None), "night"),
                      (getattr(SC, "ask_memory", None), "ask")):
        if not hook:
            continue
        try:
            s["fragments"] = list(s["fragments"]) + list(hook(role_id, ROOM.get(key) or {}, seq) or [])
        except Exception:                  # noqa: BLE001
            pass
    return s


@app.post("/api/reveal-card")
def reveal_card(b: CardOnly):
    with LOCK:
        c = SC.get_card(b.cardId)
        if not c:
            return JSONResponse({"error": "없는 카드"}, status_code=404)
        cr = current_round(ROOM["seq"])
        if c["round"] > cr:
            return JSONResponse({"error": f"아직 조사할 수 없습니다 (조사 R{c['round']}에 열림)"}, status_code=409)
        req = c.get("requires")
        if req and req not in ROOM["revealed"]:
            rq = SC.get_card(req)
            return JSONResponse({"error": f"먼저 '{rq['title'] if rq else req}'가 필요합니다"}, status_code=409)
        # 예전엔 여기서 revealed에 직접 밀어 넣었다. 그래서 이 경로로 연 카드는
        # 포드 개방도, 사건 기록도, 컷도 붙지 않았다 — 공개는 _publish 한 곳으로 모은다.
        _publish(b.cardId)
    return {"card": SC.public_card(b.cardId)}


def _agent_ok(key: str) -> bool:
    return (not AGENT_KEY) or key == AGENT_KEY


def _gm_key_ok(key: str) -> bool:
    """진짜 GM 콘솔인가. _agent_ok와 달리 빈 키를 통과시키지 않는다.

    _agent_ok는 AGENT_KEY를 안 건 서버에서 빈 키에도 True를 준다. 권한을 나누는
    자리에 그걸 쓰면 가드가 아예 없는 것과 같아서, 아무 기기나 남의 방을 갈아엎을 수 있다.
    """
    return bool(AGENT_KEY) and key == AGENT_KEY


def _is_host(client_id: str) -> bool:
    return bool(client_id) and ROOM.get("host") == client_id


def _ap_for(seq: int) -> int:
    ap = int(SC.phase_by_seq(seq).get("ap", 0) or 0)
    # 물을 못 잡았으면 마지막 조사는 허리까지 잠긴 채로 한다 — 한 곳밖에 못 본다.
    cr = ROOM.get("crisis") or {}
    conf = _crisis_conf()
    if ap > 1 and conf and cr.get("solved") is False and seq > conf["seq"]:
        return ap - 1
    return ap


def _round_checks(role_id: str, rnd: int) -> int:
    """이번 조사 라운드에 이 배역이 조사한 카드 수."""
    return sum(1 for r in ROOM["checkedRound"].get(role_id, {}).values() if r == rnd)


def _belong_locs() -> list:
    return list(getattr(SC, "BELONGINGS_LOCS", []) or [])


def _belong_limit() -> int:
    return int(getattr(SC, "BELONG_LIMIT", 2))


def _bundle_of(card: dict) -> list:
    """묶음으로 열리는 카드면 같이 열릴 형제들을 돌려준다. 아니면 빈 목록.

    소지품은 한 장씩 뒤지는 물건이 아니다 — 네 사람 것을 한자리에 늘어놓고 한 번에 본다.
    그래서 한 장을 열면 그 구역·그 라운드의 넉 장이 통째로 온다.
    """
    if not card.get("shared"):
        return []
    # 묶음 이름이 있으면 그것으로 묶는다. 없으면 구역·라운드로 묶는다(옛 방식).
    b = card.get("bundle")
    if b:
        return [c for c in SC.CARDS if c.get("bundle") == b]
    return [c for c in SC.CARDS
            if c.get("shared") and not c.get("bundle")
            and c["loc"] == card["loc"] and c["round"] == card["round"]]


def _zone_lock(loc: str, rnd: int) -> str:
    """아직 못 가는 구역이면 그 자리에서 읽어줄 한 줄. 갈 수 있으면 빈 문자열."""
    z = (getattr(SC, "ZONE_LOCK", {}) or {}).get(loc)
    if z and rnd < int(z.get("until", 0)):
        return z.get("why", "아직 갈 수 없습니다.")
    return ""


def _quota_for(seq: int) -> list:
    """이번 조사 페이즈의 «구역 몫». 시나리오가 안 정했으면 빈 목록 — 그럼 아무 제한도 없다.

    한 턴에 몇 장이냐(ap)와 «그 장을 어디에 써야 하느냐»는 다른 문제다. 쉘터 1차 조사는
    두 장인데 한 장은 반드시 소지품, 한 장은 반드시 현장이다. ap만으로는 그걸 못 적는다.
    """
    return list(SC.phase_by_seq(seq).get("quota") or [])


def _quota_used(role_id: str, rnd: int) -> dict:
    """이번 라운드에 이 배역이 각 구역에서 몇 장을 열었는지 (loc → 장수)."""
    out = {}
    for cid, r in ROOM["checkedRound"].get(role_id, {}).items():
        if r != rnd:
            continue
        c = SC.get_card(cid) or {}
        loc = c.get("loc")
        if loc:
            out[loc] = out.get(loc, 0) + 1
    return out


def _quota_state(role_id: str, seq: int) -> list:
    """화면에 그대로 뿌릴 수 있는 몫 현황. 없으면 빈 목록."""
    q = _quota_for(seq)
    if not q:
        return []
    used = _quota_used(role_id, current_round(seq))
    out = []
    for b in q:
        locs = list(b.get("locs") or [])
        out.append({"label": b.get("label", ""), "locs": locs, "n": int(b.get("n", 0) or 0),
                    "used": sum(used.get(l, 0) for l in locs)})
    return out


def _quota_block(role_id: str, seq: int, loc: str) -> str | None:
    """이 배역이 지금 이 구역을 열 수 있는가. 못 열면 사람이 읽을 이유를 돌려준다."""
    st = _quota_state(role_id, seq)
    if not st:
        return None
    ordered = bool(SC.phase_by_seq(seq).get("quotaOrder"))
    for i, b in enumerate(st):
        if loc in b["locs"]:
            if b["used"] >= b["n"]:
                left = [x for x in st if x["used"] < x["n"]]
                if left:
                    return f'{b["label"]}은(는) 이번 턴 몫을 다 썼습니다 — 남은 건 「{left[0]["label"]}」입니다'
                return f'{b["label"]}은(는) 이번 턴 몫을 다 썼습니다'
            if ordered:
                # 앞 몫이 안 찼으면 뒤 몫부터 쓸 수 없다. 순서가 곧 진행 방식이다.
                prev = [x for x in st[:i] if x["used"] < x["n"]]
                if prev:
                    return f'「{prev[0]["label"]}」부터 보고 나서 갑니다'
            return None
    return "이번 조사 턴에는 볼 수 없는 구역입니다"


def _keep_goal_results() -> list:
    """'카드를 끝까지 쥐기' 목표를 쓰는 시나리오에서, 종막 시점 달성 여부를 계산한다."""
    fn = getattr(SC, "keep_goal_result", None)
    if not fn:
        return []
    out = []
    for c in SC.CHARACTERS:
        r = fn(c["id"], ROOM["hands"].get(c["id"], []), ROOM["revealed"])
        if r:
            r["name"] = c["name"]
            r["color"] = c.get("color")
            out.append(r)
    return out


def _hand_limit() -> int:
    """손패 상한 — 넘치면 넘치는 만큼 골라서 전체공개로 내려놓아야 한다."""
    return int(getattr(SC, "HAND_LIMIT", 3))


def _split_hand(role_id: str):
    """손패를 «일반 단서»와 «소지품» 두 칸으로 가른다. 상한이 서로 다르다."""
    bl = set(_belong_locs())
    clue, belong = [], []
    for cid in ROOM["hands"].get(role_id, []):
        c = SC.get_card(cid) or {}
        (belong if c.get("loc") in bl else clue).append(cid)
    return clue, belong


def _over_limit(role_id: str) -> int:
    """일반 단서 칸이 넘친 장수. 넘치면 그만큼 전체공개로 내려놓아야 한다."""
    clue, _ = _split_hand(role_id)
    return max(0, len(clue) - _hand_limit())


def _over_belong(role_id: str) -> int:
    """소지품 칸이 넘친 장수. 이쪽은 공개가 아니라 «버리는» 것으로 정리한다 —
    넷의 물건을 다 본 다음 무엇을 손에 남길지가 이 판의 선택이다."""
    if not _belong_locs():
        return 0
    _, belong = _split_hand(role_id)
    return max(0, len(belong) - _belong_limit())


def _human_roles() -> list:
    return [rid for rid, r in ROOM["roles"].items() if r["mode"] == "human" and r["clientId"]]


def _ensure_interrogate_seq() -> None:
    """토론 페이즈에 새로 들어오면 심문 예산·투표를 초기화한다(페이즈당 예산)."""
    seq = ROOM["seq"]
    ph = SC.phase_by_seq(seq)
    ig = ROOM["interrogate"]
    if ph.get("key") == "talk" and ig.get("seq") != seq:
        ROOM["interrogate"] = {"seq": seq, "used": 0, "votes": [], "bonus": False}


def _interrogate_budget() -> dict:
    """1인당 2회 + 과반수(2인이면 만장일치) 투표로 +1. 토론 페이즈마다 리셋."""
    _ensure_interrogate_seq()
    ig = ROOM["interrogate"]
    n = len(_human_roles())
    base = 2 * n
    bonus = 1 if ig["bonus"] else 0
    total = base + bonus
    need = (n // 2 + 1) if n else 0
    return {"base": base, "bonus": bonus, "total": total, "used": ig["used"],
            "remaining": max(0, total - ig["used"]), "voteNeed": need,
            "voteHave": len(ig["votes"]), "bonusGranted": ig["bonus"]}


def _holder_of(card_id: str) -> str | None:
    """그 카드를 이미 조사한 배역(없으면 None). 조사카드는 한 사람만 가진다."""
    for rid, cids in ROOM["hands"].items():
        if card_id in cids:
            return rid
    return None


# ── 하이브리드 턴 (순번 강제 + 호스트/GM 넘기기·스킵) ─────────────────────────
def _turn_order() -> list:
    """턴 순번 = 시나리오가 정의한 TURN_ORDER, 없으면 배역 등장 순.

    라운드마다 선두를 한 칸씩 돌린다 — 고정 순번이면 첫 배역이 매 라운드 먼저
    고르게 되어, 경쟁 카드(예: 보유 목표 카드)를 늘 같은 사람이 가져간다.
    """
    order = [rid for rid in (list(getattr(SC, "TURN_ORDER", None) or [c["id"] for c in SC.CHARACTERS]))
             if rid in ROOM["roles"]]
    if not order:
        return order
    shift = max(0, current_round(ROOM["seq"]) - 1) % len(order)
    return order[shift:] + order[:shift]


def _reset_turn_for_seq(seq: int) -> None:
    """조사 페이즈에 들어오면 순번 첫 배역으로, 아니면 턴 없음."""
    ph = SC.phase_by_seq(seq)
    prev, ROOM["seq"] = ROOM["seq"], seq          # 순번 회전은 새 seq 기준으로 계산한다
    order = _turn_order()
    ROOM["seq"] = prev
    ROOM["turn"] = order[0] if (int(ph.get("ap", 0) or 0) > 0 and order) else None


def _advance_turn() -> None:
    """다음 순번으로. 이번 라운드 AP를 다 쓴 배역은 건너뛴다(한 바퀴 안에서)."""
    order = _turn_order()
    if not order:
        ROOM["turn"] = None
        return
    ap = _ap_for(ROOM["seq"])
    cur = current_round(ROOM["seq"])
    start = order.index(ROOM["turn"]) if ROOM.get("turn") in order else -1
    for step in range(1, len(order) + 1):
        cand = order[(start + step) % len(order)]
        if ap > 0 and _round_checks(cand, cur) >= ap:
            continue                                   # 이번 라운드 몫을 다 썼다
        # 열 수 있는 자리가 하나도 없으면 그 차례는 건너뛴다. 카드보다 턴이 많은 판에서
        # 아무것도 못 하는 화면 앞에 사람을 세워두면 판이 거기서 멈춘 것처럼 보인다.
        if ap > 0 and not _openable_cards(cand):
            continue
        ROOM["turn"] = cand
        bump()
        return
    ROOM["turn"] = order[(start + 1) % len(order)]      # 전원 소진 → 그냥 다음 배역
    bump()


# ── AI 자동 조사 (API 없이 휴리스틱 · 인물답게 + 추리 따라가기) ────────────────
def _evidence_bites(card_id: str, evid_id: str) -> bool:
    """들이민 카드가 지금 묻는 카드의 「짝」인가.

    예전에는 시나리오가 지정한 rebuttal 한 장만 통했고, 그 외에는 무엇을 대든
    확률이 오히려 떨어졌다. 제대로 맞물리는 카드를 찾아내고도 다른 짝을 골랐다는
    이유로 더 불리해지는 건, 추리를 벌주는 규칙이다.
    """
    if not card_id or not evid_id:
        return False
    for p in getattr(SC, "CARD_PAIRS", []) or []:
        cs = p.get("cards") or []
        if card_id in cs and evid_id in cs:
            return True
    for c in getattr(SC, "CARDS", []) or []:
        cs = c.get("combo") or []
        if card_id in cs and evid_id in cs:
            return True
    return False


def _points_at(card_id: str) -> list:
    """이 카드가 가리키는 사람들. 시나리오의 INTERROGATE 표가 곧 그 태그다 —
    「이 사람에게 이 카드를 들이밀면 할 말이 있다」가 그 표에 적혀 있다."""
    if not card_id:
        return []
    return [rid for rid, d in (getattr(SC, "INTERROGATE", {}) or {}).items() if card_id in d]


def _card_needs(c: dict) -> list:
    """이 카드를 열기 전에 먼저 나와 있어야 하는 카드들.

    requires는 한 장, combo는 여러 장이다. combo는 여태 아무도 읽지 않아서,
    「둘을 맞춰야 열리는 카드」가 실제로는 라운드만 되면 그냥 열렸다.
    """
    req = c.get("requires")
    out = [req] if isinstance(req, str) and req else list(req or [])
    out += list(c.get("combo") or [])
    return out


DEBUG_POOL = os.environ.get("PMB_DEBUG_POOL") == "1"


def _round_open_pool() -> list:
    """이번 라운드에 아직 아무도 안 가져간, 열릴 수 있는 카드. 몫·차례는 안 본다 —
    「이 판에 열 자리가 더 남았는가」만 센다."""
    cur = current_round(ROOM["seq"])
    taken = set(ROOM["revealed"])
    for cids in ROOM["hands"].values():
        taken.update(cids)
    out = []
    for c in getattr(SC, "CARDS", []) or []:
        if c["id"] in taken or c.get("round", 1) > cur or c.get("auto"):
            continue
        if c.get("gone") and cur >= c["gone"]:      # 판에서 치워진 자리
            continue
        if _zone_lock(c.get("loc", ""), cur) or c.get("loc") in (ROOM.get("sealed") or []):
            continue
        if [r for r in _card_needs(c) if r not in taken]:
            continue
        out.append(c["id"])
    return out


def _openable_cards(role_id: str) -> list:
    cur = current_round(ROOM["seq"])
    qst = _quota_state(role_id, ROOM["seq"])
    full = set()
    for b in qst:
        if b["used"] >= b["n"]:
            full.update(b["locs"])
    quota_locs = set()
    for b in qst:
        quota_locs.update(b["locs"])
    # 순서가 정해진 페이즈면, 아직 차례가 아닌 몫도 후보에서 뺀다.
    # 안 그러면 AI가 막힌 구역을 골랐다가 거절당하고 그 라운드를 통째로 흘린다 — 실제로 그랬다.
    if bool(SC.phase_by_seq(ROOM["seq"]).get("quotaOrder")):
        for i, b in enumerate(qst):
            if b["used"] < b["n"]:
                for later in qst[i + 1:]:
                    full.update(later["locs"])
                break
    mine = ROOM["hands"].get(role_id, [])
    seen = set(ROOM["revealed"])
    for cids in ROOM["hands"].values():
        seen.update(cids)
    out, fresh = [], []
    for c in SC.CARDS:
        if c["id"] in mine or c["id"] in ROOM["revealed"] or c["round"] > cur:
            continue
        if c.get("auto"):            # 판이 스스로 여는 자리 — 뒤져서 열 수 없다
            continue
        if c.get("gone") and cur >= c["gone"]:      # 이 라운드에는 이미 치워진 자리
            continue
        if _holder_of(c["id"]) and not c.get("shared"):   # 남이 가져간 카드는 후보에서 제외
            continue
        if c.get("shared") and c["id"] not in mine and _holder_of(c["id"]):
            fresh.append(c["id"])                          # 남이 이미 연 묶음 — 뒤로 미룬다
        if _zone_lock(c.get("loc", ""), cur):             # 아직 못 가는 구역
            continue
        if qst and (c["loc"] in full or c["loc"] not in quota_locs):
            continue
        if any(r not in seen for r in _card_needs(c)):
            continue
        out.append(c)
    # 자기 것은 «남의 것이 하나라도 남아 있을 때만» 뺀다. 무조건 빼면 마지막 차례에
    # 자기 카드만 남은 배역이 후보 0이 되어 턴을 그냥 흘린다 — 실제로 그랬다.
    if any(c.get("owner") != role_id for c in out):
        out = [c for c in out if c.get("owner") != role_id]
    # 아직 아무도 안 연 묶음이 남아 있으면 그쪽부터 고른다. 열린 묶음을 또 여는 건
    # 조사턴 하나를 버리는 것이고, 그러면 넷이 겹쳐서 나머지 묶음이 통째로 안 열린다.
    untouched = [c for c in out if c["id"] not in fresh]
    return untouched or out


def _recent_text(n: int = 8) -> str:
    """최근 '사람과 인물이 한 말'만 모은다. GM 안내와 페이즈 지문은 뺀다 —
    그 지문에는 이번 라운드에 볼 것들이 미리 적혀 있어서, 걸러내지 않으면
    아무도 입에 올린 적 없는 물건이 화제인 것처럼 잡힌다."""
    said = [m for m in ROOM["table"] if m.get("kind") in ("human", "ai")]
    return " ".join((m.get("text") or "") for m in said[-n:])


def _title_tokens(card: dict) -> list[str]:
    """카드 제목·위치에서 대화에 나올 만한 낱말을 뽑는다.

    형태소 분석기 없이 부분문자열로 맞춘다 — 한국어는 조사가 붙어 늘어나므로
    '단말기'는 '단말기가/단말기를'에도 걸린다. 짧은 토막은 오탐이 나서 버린다.
    """
    out = []
    for part in (card.get("title", "") + " " + card.get("spot", "")).replace("·", " ").split():
        w = part.strip("()[],.의를을이가는은도만")
        if len(w) >= 2 and not w.isdigit():
            out.append(w)
    return out


def _topic_boost(role_id: str) -> dict:
    """지금 대화가 향하는 곳을 카드 단위로 환산한다.

    구역만 보던 신호(_hot_locs)로는 '단말기 얘기 중'이나 '지금 유태오가 몰리는 중'
    같은 흐름을 못 읽는다. 그래서 카드 제목의 낱말과 사람 이름까지 본다.
    LLM 없이 도는 부분이라 대화가 붙는 만큼만 정확하다 — 그 정도면 충분하다.
    """
    txt = _recent_text(8)
    if not txt:
        return {}
    boost = {}
    for c in SC.CARDS:
        hit = sum(1 for w in _title_tokens(c) if w in txt)
        if hit:
            boost[c["id"]] = boost.get(c["id"], 0) + 2.2 * hit
    # 지금 몰리고 있는 사람은 자기 구역을 뒤져 방어할 거리를 찾는다
    me = SC.get_character(role_id)
    if me and txt.count(me["name"]) >= 2:
        home = set(((getattr(SC, "INVEST_AI", {}) or {}).get(role_id, {})).get("home", []))
        for c in SC.CARDS:
            if c["loc"] in home:
                boost[c["id"]] = boost.get(c["id"], 0) + 1.8
    return boost


def _hot_locs() -> dict:
    """최근 대화·공개에서 언급된 구역 = 추리가 향하는 곳."""
    locs = {}
    for m in ROOM["table"][-8:]:
        txt = m.get("text", "") or ""
        # 한 발언에서 같은 구역은 한 번만 센다 — 구역별로 카드 수만큼 가산되면
        # 카드가 많은 구역이 과열돼 전원이 그리로 몰린다.
        for loc in {c["loc"] for c in SC.CARDS if c["locName"] and c["locName"] in txt}:
            locs[loc] = locs.get(loc, 0) + 1
    for cid in ROOM["revealed"][-4:]:
        c = SC.get_card(cid)
        if c:
            locs[c["loc"]] = locs.get(c["loc"], 0) + 1
    return locs


def _ai_pick(role_id: str, n: int) -> list:
    """AI 배역이 성향+합리성에 따라 카드 n장을 자동으로 조사한다(즉시, API 0)."""
    prof = (getattr(SC, "INVEST_AI", {}) or {}).get(role_id, {})
    home = set(prof.get("home", []))
    interest = prof.get("interest", {})   # cardId -> 가중치(음수면 회피)
    role_kind = prof.get("role", "normal")
    keepset = set(((getattr(SC, "KEEP_GOALS", {}) or {}).get(role_id) or {}).get("cards", []))
    hot = _hot_locs()
    topic = _topic_boost(role_id)          # 대화가 지금 가리키는 카드들
    cur = current_round(ROOM["seq"])
    loc_count = {}
    for cid in ROOM["hands"].get(role_id, []):
        c = SC.get_card(cid)
        if c:
            loc_count[c["loc"]] = loc_count.get(c["loc"], 0) + 1
    picks = []
    for _ in range(max(0, n)):
        cands = [c for c in _openable_cards(role_id) if c["id"] not in picks]
        if not cands:
            break

        def score(c):
            s = 1.0
            if c["loc"] in home:
                s += 3.0
            s += interest.get(c["id"], 0)              # 관심(+)·회피(−)
            if c["id"] in keepset:
                s += 6.0                                # 자기 점수가 걸린 카드는 무엇보다 먼저 집는다
            if c["round"] == cur:
                s += 1.2                                # 이번 라운드 새 카드
            s += 0.5 * hot.get(c["loc"], 0)             # 추리 따라가기(과하면 전원이 한 구역에 몰린다)
            s += topic.get(c["id"], 0)                  # 방금 입에 오른 물건을 직접 보러 간다
            s -= 0.8 * loc_count.get(c["loc"], 0)       # 같은 구역 과다 회피
            if role_kind == "troll" and c.get("bait"):
                s += 2.5                                # 진범: 미끼로 유도
            # 재현가능 tie-break — 파이썬 str hash()는 프로세스마다 값이 달라 재현이 안 된다.
            s += (zlib.crc32(f'{ROOM["seq"]}|{role_id}|{c["id"]}'.encode()) % 97) / 970.0
            return s

        best = max(cands, key=score)
        if _try_investigate(role_id, best["id"], enforce_turn=False):
            break
        picks.append(best["id"])
        loc_count[best["loc"]] = loc_count.get(best["loc"], 0) + 1
    _ai_trim_hand(role_id)
    return picks


def _fire_cut(key: str) -> None:
    """조사 중 컷을 하나 띄운다. 같은 키는 판당 한 번만.

    상태에 실어 보내면 각 화면이 «아직 안 본 것»을 골라 재생한다. 서버가 재생을
    강제하지 않는 건, 카드를 읽는 중에 화면을 뺏기면 그게 더 방해라서다.
    """
    fn = getattr(SC, "event_cut", None)
    if not fn:
        return
    q = ROOM.setdefault("cuts", [])
    if any(c["id"] == key for c in q):
        return
    try:
        cuts = fn(key)
    except Exception:                           # noqa: BLE001
        cuts = None
    if not cuts:
        return
    q.append({"id": key, "cuts": cuts})
    del q[:-4]                                  # 늦게 들어온 화면이 옛 컷을 몰아 보는 일은 없게
    bump()


def _queue_act(role_id: str, kind: str, **fmt) -> None:
    """AI 배역에게 「지금 이 말을 하라」를 예약한다. 잠금은 부르는 쪽이 쥔다.

    자발 대화는 정적이 흘러야 시작하지만 액션은 사건 직후에 붙어야 뜻이 산다 —
    카드를 내려놓고 한참 뒤에 해석을 말하면 무슨 카드 얘긴지 아무도 모른다.
    """
    q = ROOM.setdefault("aiActs", [])
    if any(a["roleId"] == role_id and a["kind"] == kind for a in q):
        return                                  # 같은 배역이 같은 결로 줄 서 있으면 하나면 된다
    if len(q) >= 4:                             # 밀리면 판이 AI 독백이 된다
        return
    q.append({"roleId": role_id, "kind": kind, "fmt": fmt})


def _public_suspect(exclude: str = "") -> tuple[str, str, str]:
    """공개된 카드만으로 지금 가장 많이 지목된 사람과, 그 근거 문장. 손패는 절대 안 센다.

    근거는 카드의 hint(손으로 써둔 해석)를 그대로 쓴다 — 모델이 없는 사실을 지어내는 것보다
    작가가 적어둔 한 줄을 물려주는 편이 훨씬 낫다.
    """
    fn = getattr(SC, "public_suspicion", None)
    if not fn:
        return ("", "", "")
    try:
        cnt = fn(list(ROOM["revealed"]))
    except Exception:                           # noqa: BLE001
        return ("", "", "")
    cnt = {k: v for k, v in cnt.items() if k != exclude and k in ROOM["roles"]}
    if not cnt:
        return ("", "", "")
    top = max(cnt.values())
    if top < 2:                                 # 한 장 걸린 정도로 사람을 몰면 근거가 얇다
        return ("", "", "")
    tied = sorted(k for k, v in cnt.items() if v == top)
    who = tied[zlib.crc32(str(len(ROOM["revealed"])).encode()) % len(tied)]
    pts = getattr(SC, "CARD_POINTS_AT", {}) or {}
    why = []
    for cid in ROOM["revealed"]:
        if who in pts.get(cid, []):
            c = SC.get_card(cid)
            if c:
                why.append(f'「{c["title"]}」 — {(c.get("hint") or "").strip()}')
    # 모델에는 최근 두 장까지, 대체 대사에는 한 장만 — 두 장을 다 읊으면 대사가 문단이 된다.
    return (who, " / ".join(why[-2:]), (why[-1] if why else ""))


def _act_nudge(kind: str, fmt: dict) -> str:
    tpl = (getattr(SC, "CHAT_NUDGES", {}) or {}).get(kind, "")
    try:
        return tpl.format(**fmt)
    except Exception:                           # noqa: BLE001 — 자리표시자가 안 맞아도 넘어간다
        return tpl


def _act_fallback(kind: str, fmt: dict, seed: str, role_id: str = "") -> str:
    """모델이 없을 때 대신 나가는 한 줄.

    예전에는 사건 전체가 세 줄을 돌려썼다. 여섯 사람이 번갈아 「저 혼자 이렇게 읽는 겁니까?」를
    반복하니 사람이 아니라 장치라는 게 바로 보였다. 배역마다 자기 말투의 문장을 따로 두고,
    없으면 공용으로 떨어진다.
    """
    by_role = (getattr(SC, "CHAT_FALLBACK_BY_ROLE", {}) or {}).get(role_id, {})
    lines = by_role.get(kind) or (getattr(SC, "CHAT_FALLBACK", {}) or {}).get(kind) or []
    if not lines:
        return ""
    try:
        return lines[zlib.crc32(seed.encode()) % len(lines)].format(**fmt)
    except Exception:                           # noqa: BLE001
        return ""


def _ai_trim_hand(role_id: str) -> list:
    """손패 상한을 넘으면 AI가 알아서 내려놓는다.
    자기에게 불리한 카드(interest 음수 = 감추고 싶은 것)는 끝까지 쥐고, 무해하거나 공유해도 될 것부터 공개한다."""
    prof = (getattr(SC, "INVEST_AI", {}) or {}).get(role_id, {})
    interest = prof.get("interest", {})
    hide = set(prof.get("hide", []))     # 손에 들어오면 끝까지 감추는 카드
    # 끝까지 쥐어야 점수가 되는 카드는 무슨 일이 있어도 안 내려놓는다.
    # 이게 빠져 있어서, 목표 카드가 hide 목록에 우연히 겹치지 않은 배역은
    # 손패가 차는 순간 자기 목표를 스스로 공개해버렸다 — 달성률이 0이었다.
    hide |= set(((getattr(SC, "KEEP_GOALS", {}) or {}).get(role_id) or {}).get("cards", []))
    out = []
    # 소지품 칸은 «버려서» 맞춘다. 공개할 물건이 아니라 넷이 같이 본 물건이라서다.
    while _over_belong(role_id) > 0:
        _, belong = _split_hand(role_id)
        if not belong:
            break
        drop = max(belong, key=lambda cid: (interest.get(cid, 0.0),
                                            zlib.crc32(f"{role_id}|b|{cid}".encode()) % 97))
        ROOM["hands"][role_id].remove(drop)
    while _over_limit(role_id) > 0:
        hand, _ = _split_hand(role_id)
        if not hand:
            break
        # hide 목록은 마지막까지 쥔다. 그 밖에서는 interest 가 높을수록 먼저 내려놓는다.
        drop = max(hand, key=lambda cid: (-1 if cid in hide else 0,
                                          interest.get(cid, 0.0),
                                          zlib.crc32(f"{role_id}|{cid}".encode()) % 97))
        _publish_from(role_id, drop)
        out.append(drop)
        c = SC.get_card(drop)
        if c and ROOM["roles"].get(role_id, {}).get("mode") == "ai":
            # 제목만 넘기면 「이거 어떻게들 보세요」밖에 안 나온다. 본문과 손으로 써둔 해석을 같이 준다.
            # 그 배역이 이 카드를 볼 줄 아는 사람이면 자기 눈으로 읽은 것을 말하게 한다.
            # 카드에 딸린 일반 해석(hint)은 누가 말해도 똑같아서 금방 티가 난다.
            eye = (c.get("insight") or {}).get(role_id) or ""
            _queue_act(role_id, "reveal", card=c["title"],
                       body=" ".join((c.get("text") or "").split()),
                       hint=" ".join((eye or c.get("hint") or "").split()),
                       eye="1" if eye else "")
    return out


def _crisis_blocking() -> bool:
    """지금 침수 대응 판정이 걸려 있는가. 걸려 있으면 조사도 차례 넘김도 멈춘다."""
    cr = ROOM.get("crisis") or {}
    return bool(cr.get("open")) and cr.get("solved") is None


def _try_investigate(role_id: str, card_id: str, enforce_ap: bool = True, enforce_turn: bool = False) -> str | None:
    if _crisis_blocking():
        conf = _crisis_conf() or {}
        return f"「{conf.get('title', '비상')}」부터 넘겨야 합니다 — 그 사이에는 아무것도 못 뒤집니다"
    c = SC.get_card(card_id)
    if not c:
        return "없는 카드"
    cur = current_round(ROOM["seq"])
    lock = _zone_lock(c.get("loc", ""), cur)
    if lock:
        return lock
    if c.get("auto") and card_id not in ROOM["revealed"]:
        return "여기는 뒤져서 여는 자리가 아닙니다 — 때가 되면 판이 스스로 엽니다"
    if c.get("gone") and cur >= c["gone"] and card_id not in ROOM["hands"].get(role_id, []):
        return "그 자리는 이제 없습니다 — 어제와 같은 방이 아닙니다"
    if c["round"] > cur:
        return f"아직 조사할 수 없습니다 (조사 R{c['round']}에 열림)"
    # 선행 카드 검사. 여태 _openable_cards(=화면 표시와 AI 선택)에만 있었고 실제 조사 요청에는
    # 없었다. 그래서 화면이 잠가둔 카드도 요청만 보내면 그냥 열렸다.
    if card_id not in ROOM["hands"].get(role_id, []):
        seen = set(ROOM["revealed"])
        for cids in ROOM["hands"].values():
            seen.update(cids)
        miss = [r for r in _card_needs(c) if r not in seen]
        if miss:
            names = ", ".join((SC.get_card(m) or {}).get("spot") or m for m in miss)
            return f"먼저 밝혀져야 할 것이 있습니다 — {names}"
    if c.get("loc") in (ROOM.get("sealed") or []) and card_id not in ROOM["hands"].get(role_id, []):
        return f"{c.get('locName', '그 구역')}은 잠겼습니다 — 물이 차서 들어갈 수 없어요"
    ap = _ap_for(ROOM["seq"])
    already = card_id in ROOM["hands"].get(role_id, [])
    holder = _holder_of(card_id)
    if c.get("shared"):
        holder = None            # 소지품은 넷이 함께 늘어놓고 본다 — 먼저 본 사람이 가져가는 물건이 아니다
    if holder and holder != role_id:
        # 조사카드는 한 사람만 가진다 — 먼저 조사한 사람에게 물어봐야 한다.
        h = SC.get_character(holder) or {}
        return f"이미 {h.get('name', '다른 배역')}가 조사한 카드예요 — 그 사람에게 물어보세요"
    if enforce_ap and not already:
        if ap <= 0:
            return "지금은 조사 턴이 아닙니다 (조사 페이즈에서만 열 수 있어요)"
        if enforce_turn and ROOM.get("turn") and role_id != ROOM["turn"]:
            t = SC.get_character(ROOM["turn"]) or {}
            return f"지금은 {t.get('name', '다른 배역')} 차례예요 — 순서를 기다려 주세요"
        if _round_checks(role_id, cur) >= ap:
            return f"이번 조사 턴({cur}라운드)에 열 수 있는 {ap}장을 모두 사용했습니다"
        qb = _quota_block(role_id, ROOM["seq"], c.get("loc", ""))
        if qb:
            return qb
        if c.get("owner") == role_id:
            # 남의 것이 하나라도 남아 있으면 자기 것은 못 연다. 마지막 사람이 자기 카드만
            # 남은 채로 턴을 못 쓰는 일은 없어야 해서, 대안이 없을 때만 통과시킨다.
            others = [x for x in SC.CARDS
                      if x.get("loc") == c.get("loc") and x["round"] <= cur
                      and x.get("owner") != role_id
                      and x["id"] not in ROOM["revealed"] and not _holder_of(x["id"])]
            if others:
                return "자기 자신은 뒤질 수 없습니다 — 다른 사람을 고르세요"
    # 선행조건은 테이블 전체 기준 — 조사카드는 한 사람만 갖지만, 누군가 찾아낸 사실은
    # 대화로 공유되므로 그 뒤를 다른 사람이 이어 팔 수 있어야 한다.
    req = c.get("requires")
    seen = set(ROOM["revealed"])
    for cids in ROOM["hands"].values():
        seen.update(cids)
    if req and req not in seen:
        rq = SC.get_card(req)
        return f"먼저 '{rq['title'] if rq else req}'가 필요합니다"
    if card_id in ROOM["revealed"]:
        return None
    h = ROOM["hands"].setdefault(role_id, [])
    if card_id not in h:
        # 묶음이면 형제들이 같이 온다. 조사턴은 그래도 한 번만 센다 —
        # 라운드 기록은 대표 한 장에만 남기고 나머지는 0라운드로 둔다.
        sibs = _bundle_of(c)
        h.append(card_id)
        ROOM["checkedRound"].setdefault(role_id, {})[card_id] = cur
        for x in sibs:
            if x["id"] not in h:
                h.append(x["id"])
                ROOM["checkedRound"][role_id][x["id"]] = 0
        # 「누가 어디를 봤는가」는 여전히 공개 정보지만, 대화창에 적지는 않는다.
        # 조사 페이즈에는 이 줄이 사람 수 × AP 만큼 쏟아져서 정작 오간 말을 밀어냈다.
        # 그 정보를 읽는 자리는 조사 현황판이다 — 거기 카드마다 「○○ 쥐고 있음」이
        # 적혀 있고, 그건 대화가 흐르고 나서도 안 사라진다.
        _auto_combine()
        bump()
    return None


def _mark_toggle(role_id: str, card_id: str) -> str | None:
    """GM 마킹 토글: 내용은 반환하지 않는다(진행자는 카드 내용을 볼 수 없음)."""
    if role_id not in ROOM["roles"]:
        return "없는 배역"
    h = ROOM["hands"].setdefault(role_id, [])
    if card_id in h:  # 마킹 해제
        h.remove(card_id)
        ROOM["checkedRound"].get(role_id, {}).pop(card_id, None)
        bump()
        return None
    # GM 마킹은 진행자가 테이블에서 벌어진 일을 그대로 옮겨 적는 것이라 조사 턴 제한을 걸지 않는다.
    # 걸어두면 토론 페이즈(AP 0)에서 잘못 푼 마킹을 되돌릴 수가 없다 — 실제로 그래서 막혔다.
    return _try_investigate(role_id, card_id, enforce_ap=False)


def _subj(name: str) -> str:
    """이름 뒤 조사 — 받침이 있으면 '이', 없으면 '가'."""
    if not name:
        return "가"
    ch = name[-1]
    return "이" if ("가" <= ch <= "힣" and (ord(ch) - 0xAC00) % 28) else "가"


def _obj(word: str) -> str:
    """목적격 조사 — 받침이 있으면 '을', 없으면 '를'."""
    if not word:
        return "를"
    ch = word[-1]
    return "을" if ("가" <= ch <= "힣" and (ord(ch) - 0xAC00) % 28) else "를"


def _publish_from(role_id: str, card_id: str) -> None:
    """그 배역의 손패에서 카드를 빼 전체공개로 돌린다.

    대화창에는 안 적는다. 조사가 끝날 때마다 각자 두 장씩 내려놓는 판이라
    이 줄만 열 몇 개가 연달아 붙었고, 그 사이에 오간 말이 위로 밀려 사라졌다.
    무엇이 공개됐는지는 조사 현황판과 손패 화면이 계속 들고 있다.
    """
    _publish(card_id, by=role_id)
    bump()


def _auto_combine() -> None:
    """반쪽 둘이 한자리에 모이면 조합 카드가 저절로 열린다.

    맞물린다는 것을 눈으로 보고도 다시 조사 한 장을 써서 열어야 했는데, 그건 발견의
    대가가 아니라 확인 절차다. 둘이 다 테이블에 있으면 테이블에서 열리고, 한 사람 손에
    다 있으면 그 사람 손에 들어온다. 손패 상한은 그대로다 — 넘치면 넘친 채로 들어오고,
    다른 카드를 내려놓아야 한다(사람은 경고를 보고, AI는 스스로 정리한다).
    """
    for c in getattr(SC, "CARDS", []) or []:
        need = list(c.get("combo") or [])
        if not need:
            continue
        cid = c["id"]
        if cid in ROOM["revealed"] or _holder_of(cid):
            continue
        # 라운드 잠금은 걸지 않는다. 반쪽 둘을 모으는 것 자체가 이미 관문이고,
        # 그걸 해내고도 라운드를 기다리라는 건 잠금을 두 번 거는 셈이다.
        if all(x in ROOM["revealed"] for x in need):
            ROOM["table"].append({"kind": "system", "broadcast": True,
                                  "text": f'테이블 위의 두 조각이 맞물렸습니다 — 「{c["title"]}」이(가) 드러납니다.'})
            _publish(cid)
            continue
        for rid, hl in ROOM["hands"].items():
            if all(x in hl for x in need):
                hl.append(cid)
                ROOM["checkedRound"].setdefault(rid, {})[cid] = current_round(ROOM["seq"])
                nm = (SC.get_character(rid) or {}).get("name", rid)
                ROOM["table"].append({"kind": "system", "broadcast": True,
                                      "text": f'{nm}{_subj(nm)} 쥐고 있던 두 조각을 맞췄습니다 — 새 단서가 그 손에 들어왔습니다.'})
                _ev("combine", roleId=rid, cardId=cid, title=c["title"])
                break
    bump()


def _publish(card_id: str, by: str = "") -> None:
    """공개는 여기 한 곳으로 모인다 — 사건 기록도 여기서 낸다.
    호출 경로가 여럿이라(본인 공개·GM 공개·정리) 위쪽에서 내면 빠지는 길이 생긴다."""
    for hl in ROOM["hands"].values():
        if card_id in hl:
            hl.remove(card_id)
    if card_id not in ROOM["revealed"]:
        ROOM["revealed"].append(card_id)
        c = SC.get_card(card_id)
        if c and c.get("unlocks") == "pod" and not ROOM.get("podOpen"):
            ROOM["podOpen"] = True
            ROOM["table"].append({"kind": "system", "broadcast": True,
                                  "text": "계통도가 가리키던 것이 드러났습니다 — 배치도에 탈출 포드가 표시됩니다."})
            _ev("unlock", what="pod", cardId=card_id)
            _fire_cut("pod")
        if c:
            who = SC.get_character(by) or {}
            _ev("reveal", roleId=by, speaker=who.get("name", ""), cardId=card_id,
                title=c["title"], loc=c["loc"], locName=c["locName"], spot=c.get("spot", ""),
                text=c.get("text", ""), hint=c.get("hint", ""))
        bump()
        _auto_combine()


def _ai_take_turn(rid: str) -> list:
    """AI 배역의 조사 차례를 한 번에 처리한다 — 이번 라운드에 남은 만큼 뽑고 다음 차례로 넘긴다.

    진행석 버튼(/api/ai-investigate)과 배경 스레드(_auto_turn_tick)가 같은 이 함수를 쓴다.
    잠긴 구역·선행 단서·남이 이미 가져간 카드 판정은 전부 _try_investigate 안에 있으므로
    여기서 따로 손대지 않는다. LOCK을 쥔 채로 부를 것(LLM 호출 없이 즉시 끝난다).
    """
    remaining = _ap_for(ROOM["seq"]) - _round_checks(rid, current_round(ROOM["seq"]))
    picks = _ai_pick(rid, remaining)
    # 어디를 살펴봤는지는 _try_investigate가 한 장씩 테이블에 남긴다(사람이든 AI든 같은 길).
    if ROOM.get("turn") == rid:
        _advance_turn()
    bump()
    return picks


@app.post("/api/investigate")
def investigate(b: Investigate):
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId:
            return JSONResponse({"error": "그 배역으로 조사할 수 없습니다"}, status_code=403)
        err = _try_investigate(b.roleId, b.cardId, enforce_turn=True)
        if err:
            return JSONResponse({"error": err}, status_code=409)
        # 이번 턴 AP를 다 썼으면 자동으로 다음 차례로
        ap = _ap_for(ROOM["seq"])
        if ap > 0 and ROOM.get("turn") == b.roleId and _round_checks(b.roleId, current_round(ROOM["seq"])) >= ap:
            _advance_turn()
    return {"card": SC.public_card(b.cardId)}


@app.post("/api/mark")
def mark(b: AgentCard):
    """진행자(GM) 마킹 — 어떤 배역이 어떤 카드를 조사했는지 토글. 카드 내용은 반환하지 않음."""
    if not _agent_ok(b.key):
        return JSONResponse({"error": "key"}, status_code=403)
    with LOCK:
        if not b.roleId:
            return JSONResponse({"error": "배역 필요"}, status_code=400)
        err = _mark_toggle(b.roleId, b.cardId)
        if err:
            return JSONResponse({"error": err}, status_code=409)
        checked = b.cardId in ROOM["hands"].get(b.roleId, [])
    return {"ok": True, "checked": checked}


@app.post("/api/publish")
def publish_card(b: Investigate):
    """손패에서 카드 한 장을 전체공개로 내려놓는다(손패 상한 정리)."""
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId:
            return JSONResponse({"error": "권한 없음"}, status_code=403)
        if b.cardId not in ROOM["hands"].get(b.roleId, []):
            return JSONResponse({"error": "내 손패에 없는 카드입니다"}, status_code=409)
        _publish_from(b.roleId, b.cardId)
    return {"ok": True, "over": _over_limit(b.roleId)}


@app.post("/api/belongings/keep")
def belongings_keep(b: SwapCard):
    """소지품 칸을 상한까지 줄인다. b.giveId 에 «남길 카드 id들»을 쉼표로 잇는다.

    넷의 물건을 한자리에 늘어놓고 본 다음, 손에 남길 것만 고른다.
    안 고른 것은 공개되지 않고 그냥 사라진다 — 봤다는 사실만 남는다.
    """
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId:
            return JSONResponse({"error": "권한 없음"}, status_code=403)
        keep = [x for x in (b.giveId or "").split(",") if x]
        lim = _belong_limit()
        if len(keep) > lim:
            return JSONResponse({"error": f"{lim}장까지만 남길 수 있습니다"}, status_code=409)
        _, belong = _split_hand(b.roleId)
        if any(k not in belong for k in keep):
            return JSONResponse({"error": "내가 지금 보고 있는 소지품이 아닙니다"}, status_code=409)
        for cid in belong:
            if cid not in keep:
                ROOM["hands"][b.roleId].remove(cid)
        bump()
    return {"ok": True, "overBelong": _over_belong(b.roleId)}


@app.post("/api/ready")
def ready_toggle(b: RoleReq):
    """「결과 확인」 토글. 사람 배역이 전원 켜면 판이 다음 막으로 넘어간다.

    호스트 한 사람이 넘기는 것과 다르다 — 종막은 아직 할 말이 남은 사람이 있기 마련이고,
    그 사람이 준비되기 전에 진상이 열리면 판이 거기서 끝나버린다.
    """
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "그 배역으로 누를 수 없습니다"}, status_code=403)
        rd = ROOM.setdefault("ready", [])
        nm = (SC.get_character(b.roleId) or {}).get("name", b.roleId)
        if b.roleId in rd:
            rd.remove(b.roleId)
        else:
            rd.append(b.roleId)
            ROOM["table"].append({"kind": "system", "broadcast": True,
                                  "text": f"{nm}{_subj(nm)} 결과 확인을 눌렀습니다."})
        st = _ready_state()
        if st and st["done"]:
            ROOM["table"].append({"kind": "system", "broadcast": True,
                                  "text": "전원이 준비됐습니다 — 다음 막으로 넘어갑니다."})
            ROOM["ready"] = []
            _advance()
        bump()
    return {"ok": True, "ready": _ready_state()}


def _ready_state():
    humans = _human_roles()
    if not humans:
        return None
    rd = [r for r in (ROOM.get("ready") or []) if r in humans]
    return {"n": len(rd), "of": len(humans), "done": len(rd) >= len(humans), "mine": list(rd)}


@app.post("/api/swap")
def swap_card(b: SwapCard):
    """전체공개된 카드 한 장을 되가져오고, 대신 내 손패 한 장을 내려놓는다.

    손패가 두 장뿐이라 «지금 감춰야 할 것»이 페이즈마다 바뀐다. 그때 이미 테이블에 나간 카드를
    다시 품을 길이 없으면 한 번의 실수가 판 끝까지 간다. 다만 공짜는 아니다 —
    되가져오는 만큼 내 것 하나가 반드시 모두의 것이 된다. 그 교환 자체가 공개 정보다.
    """
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId:
            return JSONResponse({"error": "권한 없음"}, status_code=403)
        if b.giveId not in ROOM["hands"].get(b.roleId, []):
            return JSONResponse({"error": "내 손패에 없는 카드입니다"}, status_code=409)
        if b.takeId not in ROOM["revealed"]:
            return JSONResponse({"error": "전체공개된 카드가 아닙니다"}, status_code=409)
        if b.giveId == b.takeId:
            return JSONResponse({"error": "같은 카드입니다"}, status_code=409)
        take = SC.get_card(b.takeId)
        if take and take.get("loc") in (ROOM.get("sealed") or []):
            return JSONResponse({"error": f"{take.get('locName', '그 구역')}은 잠겼습니다"}, status_code=409)
        # 먼저 집어오고 나서 내려놓는다. 순서를 뒤집으면 잠깐 상한을 넘는다.
        ROOM["revealed"].remove(b.takeId)
        ROOM["hands"].setdefault(b.roleId, []).append(b.takeId)
        ROOM["checkedRound"].setdefault(b.roleId, {})[b.takeId] = current_round(ROOM["seq"])
        _publish_from(b.roleId, b.giveId)
        nm = (SC.get_character(b.roleId) or {}).get("name", b.roleId)
        tt = take["title"] if take else b.takeId
        ROOM["table"].append({"kind": "system", "broadcast": True,
                              "text": f'{nm}{_subj(nm)} 「{tt}」{_obj(tt)} 도로 가져갔습니다.'})
        bump()
    return {"ok": True}


# ── 탈출 포드 개방 · 범인 지목 ────────────────────────────────
def _pod_code() -> str:
    return str(getattr(SC, "POD_CODE", "") or "")


def _pod_marked(cid: str) -> bool:
    """이 배역의 지도에 포드 자리가 찍혀 있는가.
    처음부터 아는 사람은 처음부터, 나머지는 「비상포드 발견」이 공개된 뒤부터."""
    if not cid:
        return False
    return bool(ROOM.get("podOpen")) or cid in (getattr(SC, "POD_KNOWERS", []) or [])


def _pod_launch_public():
    """발사창이 열린 뒤의 마지막 10초. 표는 받았는데 코드를 못 맞춘 사람에게
    그 자리에서 입력할 기회를 한 번 준다 — 못 넣으면 문이 닫힌다."""
    pl = ROOM.get("podLaunch")
    if not pl:
        return None
    left = max(0.0, pl["deadline"] - time.monotonic())
    if left <= 0 and not pl.get("closed"):
        pl["closed"] = True
        _pod_close()
    return {"boarded": list(pl["boarded"]), "left": round(left, 1),
            "closed": bool(pl.get("closed")), "escaped": list(pl.get("escaped") or [])}


def _pod_knows_code(rid: str) -> bool:
    """이 배역이 코드를 넣을 수 있는 상태인가.
    사람은 제 손으로 넣어야 한다. AI는 넣을 손이 없으므로, 숫자가 적힌 카드가
    이미 판 위에 공개돼 있으면 아는 것으로 본다 — 아니면 AI가 타는 순간 늘 실패한다."""
    if ROOM["podCode"].get(rid):
        return True
    if (ROOM["roles"].get(rid) or {}).get("mode") != "ai":
        return False
    src = getattr(SC, "POD_CODE_SOURCE", "")
    return bool(src) and src in (ROOM.get("revealed") or [])


def _pod_close():
    """문이 닫힌다. 코드를 맞춘 사람만 실제로 나간다."""
    pl = ROOM.get("podLaunch") or {}
    ok = [r for r in pl.get("boarded", []) if _pod_knows_code(r)]
    pl["escaped"] = ok
    names = [(SC.get_character(r) or {}).get("name", r) for r in ok]
    miss = [(SC.get_character(r) or {}).get("name", r)
            for r in pl.get("boarded", []) if not _pod_knows_code(r)]
    if names:
        msg = f"포드가 떠났습니다 — {', '.join(names)}."
    else:
        msg = "포드는 뜨지 못했습니다. 인증코드를 넣은 사람이 없습니다."
    if miss:
        msg += f" ({', '.join(miss)}: 코드를 넣지 못해 남습니다)"
    ROOM["table"].append({"kind": "system", "broadcast": True, "text": msg})
    bump()


def _pod_state() -> dict:
    """포드 투표 현황. 사람이 다 던지기 전에는 «누가 누구를 찍었나»를 감춘다 —
    마지막에 던지는 사람이 전부 보고 결정하면 그건 투표가 아니라 계산이다."""
    humans = _human_roles()
    voted = [r for r in humans if ROOM["podVotes"].get(r)]
    done = bool(humans) and len(voted) >= len(humans)
    st = {"seats": getattr(SC, "POD_SEATS", 3), "voters": len(humans),
          "voted": len(voted), "done": done, "mine": None,
          "hasCode": bool(_pod_code())}
    if done and hasattr(SC, "pod_result"):
        merged = dict(getattr(SC, "POD_VOTE_AI", {}) or {})
        for r in humans:                       # 사람 표가 AI 고정표를 덮는다
            merged[r] = ROOM["podVotes"][r]
        st["result"] = SC.pod_result(merged)
        st["votes"] = merged
    return st


@app.post("/api/pod/code")
def pod_code(b: PodCode):
    """포드 인증코드 입력. 맞히면 그 사실이 그 사람에게만 남는다 —
    누가 코드를 아는지는 밝히지 않는다. 그걸 알면 표가 그리로만 쏠린다."""
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId:
            return JSONResponse({"error": "그 배역이 아닙니다"}, status_code=403)
        want = _pod_code()
        if not want:
            return JSONResponse({"error": "이 사건에는 인증코드가 없습니다"}, status_code=409)
        if not _pod_marked(b.roleId):
            return JSONResponse({"error": "아직 포드 자리를 모릅니다"}, status_code=409)
        got = "".join(ch for ch in (b.code or "") if ch.isdigit())
        if got != want:
            return {"ok": False, "wrong": True}
        first = not ROOM["podCode"].get(b.roleId)
        ROOM["podCode"][b.roleId] = True
        # 발사 도중에 넣었으면 그 자리에서 태운다.
        pl = ROOM.get("podLaunch")
        if pl and not pl.get("closed") and b.roleId in pl.get("boarded", []):
            if all(_pod_knows_code(x) for x in pl["boarded"]):
                pl["closed"] = True
                _pod_close()
        if first:
            bump()
        return {"ok": True}


@app.post("/api/pod/vote")
def pod_vote(b: VoteReq):
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "그 배역으로 투표할 수 없습니다"}, status_code=403)
        if ROOM["seq"] != 7:
            return JSONResponse({"error": "포드 투표는 최종 토론에서만 할 수 있습니다"}, status_code=409)
        if b.targetRoleId == b.roleId:
            return JSONResponse({"error": "자기 자신은 태울 수 없습니다"}, status_code=409)
        if b.targetRoleId not in ROOM["roles"]:
            return JSONResponse({"error": "없는 배역"}, status_code=404)
        first = b.roleId not in ROOM["podVotes"]
        ROOM["podVotes"][b.roleId] = b.targetRoleId
        nm = (SC.get_character(b.roleId) or {}).get("name", b.roleId)
        if first:
            ROOM["table"].append({"kind": "system", "broadcast": True,
                                  "text": f"{nm}{_subj(nm)} 포드 탑승자를 정했습니다. (누구인지는 전원이 정한 뒤에 열립니다)"})
        st = _pod_state()
        if st["done"]:
            res = st["result"]
            names = [(SC.get_character(x) or {}).get("name", x) for x in res["boarded"]]
            if res["reason"] == "tie":
                msg = "발사창이 열렸지만 표가 넷 이상으로 갈렸습니다 — 자리를 나누지 못해 아무도 타지 못합니다."
            elif not names:
                msg = "아무도 표를 받지 못했습니다. 포드는 빈 채로 남습니다."
            elif len(names) == 1:
                msg = f"발사창이 열립니다. {names[0]}, 혼자 탑니다."
            else:
                msg = f"발사창이 열립니다. 타는 사람 — {', '.join(names)}."
            ROOM["table"].append({"kind": "system", "broadcast": True, "text": msg})
            # 코드를 아직 못 맞춘 탑승자에게 마지막 10초. 코드가 없는 사건이면 그냥 태운다.
            board = list(res.get("boarded") or [])
            if board and _pod_code():
                need = [r for r in board if not _pod_knows_code(r)]
                if need:
                    ROOM["podLaunch"] = {"boarded": board, "deadline": time.monotonic() + 10.0}
                    ROOM["table"].append({"kind": "system", "broadcast": True,
                                          "text": "발사에는 선장 인증코드가 필요합니다. 10초 안에 넣지 못하면 문이 닫힙니다."})
                else:
                    ROOM["podLaunch"] = {"boarded": board, "deadline": time.monotonic(), "closed": True}
                    _pod_close()
        bump()
    return {"ok": True, "pod": _pod_state()}


def _dest_state():
    """어디로 갈 것인가. 이 기믹이 있는 사건에서만 내려간다(지금은 쉘터).

    포드와 같은 규칙으로 감춘다 — 마지막에 정하는 사람이 남의 선택을 다 보고 고르면
    그건 선택이 아니라 계산이다. 전원이 정해야 한꺼번에 열린다.
    """
    dests = getattr(SC, "DESTINATIONS", None)
    if not dests:
        return None
    humans = _human_roles()
    picked = ROOM.setdefault("dest", {})   # 시나리오 전환 전에 열린 방에는 이 칸이 없다
    chosen = [r for r in humans if picked.get(r)]
    done = bool(humans) and len(chosen) >= len(humans)
    st = {"options": dests, "voters": len(humans), "chosen": len(chosen), "done": done}
    if done:
        merged = dict(getattr(SC, "DEST_AI", {}) or {})
        for r in humans:                      # 사람의 선택이 AI 기본값을 덮는다
            merged[r] = picked[r]
        arrested = ""
        ar = _arrest_state() or {}
        if ar.get("caught"):
            arrested = getattr(SC, "CULPRIT_ID", "")
        st["result"] = SC.dest_result(merged, arrested)
        # 검거된 사람은 아무 데도 못 간다. 판정에서는 이미 빠지는데 목록에는 남아서,
        # 잡혀놓고 태연히 어딘가로 떠난 것처럼 보였다.
        st["picks"] = {r: d for r, d in merged.items() if r != arrested}
        st["arrested"] = arrested
        if hasattr(SC, "ending_for"):
            st["ending"] = SC.ending_for(st["result"], arrested)
    return st


def _decision_barred() -> str:
    """결재권을 잃는 사람 — 종막에서 표가 제일 많이 몰린 배역.

    동률이면 아무도 잃지 않는다. 「누군가는 반드시 잃는다」로 만들면 표가 갈렸을 때
    임의로 한 명을 골라야 하는데, 그건 판정이 아니라 주사위다.
    """
    tally = {}
    for t in ROOM.get("accuse", {}).values():
        if t:
            tally[t] = tally.get(t, 0) + 1
    if not tally:
        return ""
    top = max(tally.values())
    lead = [r for r, n in tally.items() if n == top]
    return lead[0] if len(lead) == 1 else ""


def _decision_state():
    """결재 결정문. 이 기믹이 있는 사건에서만 내려간다(지금은 쉘터).

    행선지와 달리 «가리지» 않는다 — 결재란은 원래 앞사람 도장을 보고 찍는 자리고,
    마지막 전략란은 앞의 셋을 보고 따를지 뒤집을지 정하는 자리라서 순서가 곧 규칙이다.
    """
    seats = getattr(SC, "DECISION", None)
    if not seats:
        return None
    d = ROOM.setdefault("decision", {"picks": {}, "votes": {}, "extra": ""})
    barred = _decision_barred()
    humans = _human_roles()
    ai_def = dict(getattr(SC, "DECISION_AI", {}) or {})

    picks = {}
    pending = []          # 아직 안 채워진 칸
    for seat in seats:
        rid = seat["roleId"]
        if rid == barred:
            # 잃은 칸 — 남은 사람들의 표로 찬다. 최다득표, 동률이면 아직 미정.
            v = (d.get("votes") or {}).get(rid, {})
            tally = {}
            for opt in v.values():
                tally[opt] = tally.get(opt, 0) + 1
            voters = [r for r in humans if r != barred]
            if not voters:                       # 사람이 없으면 AI 기본값으로
                picks[rid] = ai_def.get(rid, seat["options"][0]["id"])
                continue
            if len(v) < len(voters):
                pending.append(rid); continue
            top = max(tally.values()) if tally else 0
            lead = [o for o, n in tally.items() if n == top]
            if len(lead) == 1:
                picks[rid] = lead[0]
            else:
                pending.append(rid)
        elif rid in humans:
            if d["picks"].get(rid):
                picks[rid] = d["picks"][rid]
            else:
                pending.append(rid)
        else:
            picks[rid] = ai_def.get(rid, seat["options"][0]["id"])

    ex = getattr(SC, "DECISION_EXTRA", None)
    extra = d.get("extra") or ""
    if ex:
        if ex["roleId"] in humans:
            if not extra:
                pending.append("__extra__")
        elif not extra:
            extra = getattr(SC, "DECISION_EXTRA_AI", "none")

    st = {"intro": getattr(SC, "DECISION_INTRO", ""), "seats": seats, "extraSpec": ex,
          "barred": barred, "picks": picks, "extra": extra,
          "votes": {k: len(v) for k, v in (d.get("votes") or {}).items()},
          "voters": len([r for r in humans if r != barred]),
          "pending": pending, "done": not pending}
    if st["done"]:
        st["report"] = SC.decision_report(picks, extra or "none", barred)
    return st


@app.post("/api/decision")
def decision_pick(b: VoteReq):
    """b.targetRoleId 에 «자리의 배역 id:선택지 id» 를 담아 보낸다.

    자기 자리면 자기가 고르고, 결재권을 잃은 자리면 표를 던지는 것이다 —
    두 동작이 같은 화면의 같은 버튼이라 엔드포인트도 하나로 둔다.
    """
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "그 배역으로 결재할 수 없습니다"}, status_code=403)
        if not getattr(SC, "DECISION", None):
            return JSONResponse({"error": "이 사건에는 결재가 없습니다"}, status_code=409)
        if SC.phase_by_seq(ROOM["seq"]).get("key") != "decision":
            return JSONResponse({"error": "결재는 결재 페이즈에서만 할 수 있습니다"}, status_code=409)
        seat_id, _, opt_id = (b.targetRoleId or "").partition(":")
        seat = next((x for x in SC.DECISION if x["roleId"] == seat_id), None)
        if not seat or opt_id not in [o["id"] for o in seat["options"]]:
            return JSONResponse({"error": "없는 칸이거나 없는 선택지"}, status_code=404)
        barred = _decision_barred()
        d = ROOM.setdefault("decision", {"picks": {}, "votes": {}, "extra": ""})
        nm = (SC.get_character(b.roleId) or {}).get("name", b.roleId)
        if seat_id == barred:
            if b.roleId == barred:
                return JSONResponse({"error": "결재권을 잃었습니다 — 이 칸은 남은 사람들이 채웁니다"},
                                    status_code=409)
            first = b.roleId not in d.setdefault("votes", {}).setdefault(seat_id, {})
            d["votes"][seat_id][b.roleId] = opt_id
            if first:
                ROOM["table"].append({"kind": "system", "broadcast": True,
                                      "text": f'{nm}{_subj(nm)} 「{seat["seat"]}란」에 표를 던졌습니다.'})
        else:
            if seat_id != b.roleId:
                return JSONResponse({"error": "남의 칸에는 서명할 수 없습니다 — 대리 서명은 무효입니다"},
                                    status_code=403)
            first = not d["picks"].get(seat_id)
            d["picks"][seat_id] = opt_id
            if first:
                ROOM["table"].append({"kind": "system", "broadcast": True,
                                      "text": f'{nm}{_subj(nm)} 「{seat["seat"]}란」에 서명했습니다.'})
        bump()
    return {"ok": True, "decision": _decision_state()}


@app.post("/api/decision/extra")
def decision_extra(b: VoteReq):
    """결정문과 별개로 한 사람에게만 열리는 선택(쉘터에서는 분화구행)."""
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "권한 없음"}, status_code=403)
        ex = getattr(SC, "DECISION_EXTRA", None)
        if not ex:
            return JSONResponse({"error": "이 사건에는 없는 선택입니다"}, status_code=409)
        if b.roleId != ex["roleId"]:
            return JSONResponse({"error": "당신에게 열린 선택이 아닙니다"}, status_code=403)
        if SC.phase_by_seq(ROOM["seq"]).get("key") != "decision":
            return JSONResponse({"error": "결재 페이즈에서만 정할 수 있습니다"}, status_code=409)
        if b.targetRoleId not in [o["id"] for o in ex["options"]]:
            return JSONResponse({"error": "없는 선택지"}, status_code=404)
        ROOM.setdefault("decision", {"picks": {}, "votes": {}, "extra": ""})["extra"] = b.targetRoleId
        bump()
    return {"ok": True, "decision": _decision_state()}


@app.post("/api/destination")
def choose_destination(b: VoteReq):
    """b.targetRoleId에 행선지 id를 담아 보낸다(배역이 아니라 장소다)."""
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "그 배역으로 정할 수 없습니다"}, status_code=403)
        if not getattr(SC, "DESTINATIONS", None):
            return JSONResponse({"error": "이 사건에는 행선지가 없습니다"}, status_code=409)
        if SC.phase_by_seq(ROOM["seq"]).get("key") != "final":
            return JSONResponse({"error": "행선지는 최후의 선택에서만 정할 수 있습니다"}, status_code=409)
        if b.targetRoleId not in [d["id"] for d in SC.DESTINATIONS]:
            return JSONResponse({"error": "없는 행선지"}, status_code=404)
        first = b.roleId not in ROOM.setdefault("dest", {})
        ROOM["dest"][b.roleId] = b.targetRoleId
        nm = (SC.get_character(b.roleId) or {}).get("name", b.roleId)
        if first:
            ROOM["table"].append({"kind": "system", "broadcast": True,
                                  "text": f"{nm}{_subj(nm)} 갈 곳을 정했습니다. (어디인지는 전원이 정한 뒤에 열립니다)"})
        st = _dest_state()
        if st and st["done"]:
            byd = {}
            for rid, d in (st.get("picks") or {}).items():
                byd.setdefault(d, []).append((SC.get_character(rid) or {}).get("name", rid))
            label = {d["id"]: d["name"] for d in SC.DESTINATIONS}
            lines = " · ".join(f"{label.get(d, d)} — {', '.join(ns)}" for d, ns in byd.items())
            ROOM["table"].append({"kind": "system", "broadcast": True,
                                  "text": f"길이 갈립니다. {lines}"})
        bump()
    return {"ok": True, "dest": _dest_state()}


@app.post("/api/accuse")
def accuse(b: VoteReq):
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "그 배역으로 지목할 수 없습니다"}, status_code=403)
        if SC.phase_by_seq(ROOM["seq"]).get("key") != "final":
            return JSONResponse({"error": "범인 지목은 종막에서만 할 수 있습니다"}, status_code=409)
        if b.targetRoleId not in ROOM["roles"]:
            return JSONResponse({"error": "없는 배역"}, status_code=404)
        ROOM["accuse"][b.roleId] = b.targetRoleId
        bump()
    return {"ok": True}


def _arrest_state():
    """검거 판정. 이 기믹이 없는 사건도 있다 — 잠수정만 표로 범인을 잡는다.

    예전엔 seq 8이 되면 무조건 여기까지 들어와서, arrest_result가 없는 사건은
    /api/state가 통째로 500을 냈다. 폴링이 죽으니 종막에 들어선 순간 모두의 화면이
    멈췄다. 없으면 없는 대로 None을 준다.
    """
    if not hasattr(SC, "arrest_result"):
        return None
    humans = _human_roles()
    culprit_human = (ROOM["roles"].get(SC.CULPRIT_ID) or {}).get("mode") == "human"
    res = SC.arrest_result(ROOM["accuse"], humans, culprit_human)
    res["culpritIsHuman"] = culprit_human
    res["done"] = bool(humans) and all(r in ROOM["accuse"] for r in humans)
    return res


# ── 밤 — 각자 하나를 고르고, 그 조합이 그날 밤을 정한다 ──────────────
# 이 기믹이 있는 사건은 «누가 범인인가»가 판 시작 시점에 안 정해져 있다.
# 사건 모듈이 NIGHT_ACTS(선택지)와 night_resolve(조합→결과)를 갖고 있으면 열린다.
# ── 금고 서류 — 다 함께 읽고 넘어가는 자리 ──────────────────────
def _vault_conf():
    v = getattr(SC, "VAULT_DOCS", None)
    return v if (v and v.get("docs")) else None


def _vault_open() -> bool:
    """지금 이 서류를 읽는 막인가. 밤이 판정된 뒤부터, 그 막이 끝날 때까지."""
    v = _vault_conf()
    if not v or ROOM["seq"] != int(v.get("seq", 0)):
        return False
    return bool((ROOM.get("night") or {}).get("result"))


def _vault_public(role_id: str = "") -> dict | None:
    v = _vault_conf()
    if not v or ROOM["seq"] < int(v.get("seq", 0)):
        return None
    read = list(ROOM.get("vaultRead") or [])
    # 읽는 건 사람이다. AI 배역은 기다릴 대상이 아니다 — 안 그러면 영영 안 끝난다.
    seats = [rid for rid, r in ROOM["roles"].items() if r.get("clientId")]
    out = {k: v[k] for k in ("kick", "title", "lede", "docs", "foot", "readLabel") if k in v}
    out.update({"open": _vault_open(), "read": read,
                "need": len(seats), "done": all(r in read for r in seats),
                "mine": bool(role_id and role_id in read)})
    return out


@app.post("/api/vault/read")
def vault_read(b: RoleReq):
    with LOCK:
        if not _vault_open():
            return JSONResponse({"error": "지금 읽을 서류가 없습니다"}, status_code=409)
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId:
            return JSONResponse({"error": "그 배역이 아닙니다"}, status_code=403)
        q = ROOM.setdefault("vaultRead", [])
        if b.roleId not in q:
            q.append(b.roleId)
            bump()
        return {"ok": True, "vault": _vault_public(b.roleId)}


def _night_conf():
    return getattr(SC, "NIGHT_ACTS", None)


def _night_open() -> None:
    conf = _night_conf()
    if not conf:
        return
    n = ROOM.setdefault("night", {"open": False, "picks": {}, "result": None})
    if n.get("open") or n.get("result"):
        return
    n["open"] = True
    n["picks"] = {}
    # 사람이 안 앉은 배역은 그 자리에서 제 성격대로 고른다. 밤은 기다려주지 않는다.
    for rid, r in ROOM["roles"].items():
        if r["mode"] == "ai":
            try:
                n["picks"][rid] = SC.night_ai_pick(rid)
            except Exception:                           # noqa: BLE001
                pass
    ROOM["table"].append({"kind": "system", "broadcast": True,
                          "text": conf.get("notice", "밤이 되었습니다 — 각자 화면에서 오늘 밤 무엇을 할지 고르세요.")})
    _fire_cut("night:open")
    _ev("night", state="open")
    _night_try_resolve()


def _night_try_resolve() -> None:
    """전원이 고른 뒤에만 판정한다. 진행석은 /api/night/close 로 앞당길 수 있다."""
    n = ROOM.get("night") or {}
    if not n.get("open"):
        return
    assigned = [rid for rid, r in ROOM["roles"].items() if r["mode"] in ("human", "ai")]
    if assigned and any(rid not in n["picks"] for rid in assigned):
        return
    _night_resolve()


def _night_resolve() -> None:
    conf = _night_conf()
    n = ROOM.get("night") or {}
    if not conf or not n.get("open"):
        return
    n["open"] = False
    try:
        n["result"] = SC.night_resolve(dict(n.get("picks") or {}))
    except Exception:                                   # noqa: BLE001
        n["result"] = {"killer": "", "order": [], "public": [], "headline": ""}
    # 밤이 남긴 것은 여러 줄이다. 아침에 한꺼번에 붙이면 무엇이 먼저 벌어진 일인지 안 보인다.
    with _drip():
        for line in (n["result"].get("public") or []):
            ROOM["table"].append({"kind": "system", "broadcast": True, "text": line})
    _fire_cut("night:done")
    _ev("night", state="done", killer=n["result"].get("killer", ""))
    bump()


def _night_public(role_id: str = "") -> dict | None:
    conf = _night_conf()
    if not conf:
        return None
    n = ROOM.get("night") or {}
    if not n.get("open") and not n.get("result"):
        return None
    assigned = [rid for rid, r in ROOM["roles"].items() if r["mode"] in ("human", "ai")]
    out = {"open": bool(n.get("open")), "seq": int(conf.get("seq", 0) or 0),
           "kick": conf.get("kick", ""), "title": conf.get("title", ""),
           "intro": conf.get("intro", ""), "prompt": conf.get("prompt", ""),
           "picked": sorted(n.get("picks") or {}), "total": len(assigned),
           "done": n.get("result") is not None}
    # 선택지는 그 사람 것만 내려간다 — 남이 무엇을 고를 수 있는지까지 보이면 밤이 아니다.
    if role_id:
        opts = (conf.get("options") or {}).get(role_id) or []
        out["mine"] = (n.get("picks") or {}).get(role_id, "")
        out["why"] = (conf.get("why") or {}).get(role_id, "")
        # 시각은 정하고 나서야 알려준다. 고르기 전에는 «이르게/늦게/안 간다»까지다 —
        # 몇 시인지를 미리 알면 그건 시간을 고르는 것이지 행동을 고르는 게 아니다.
        if out["mine"]:
            out["options"] = opts
        else:
            out["options"] = [{k: v for k, v in o.items() if k != "at"} for o in opts]
    if n.get("result"):
        r = n["result"]
        out["headline"] = r.get("headline", "")
        out["outcome"] = list(r.get("public") or [])
        if role_id:
            out["mineOutcome"] = (r.get("private") or {}).get(role_id, "")
            out["mineCuts"] = list((r.get("vn") or {}).get(role_id) or [])
    return out


class NightPick(BaseModel):
    roleId: str
    clientId: str
    optId: str


@app.post("/api/night")
def night_pick(b: NightPick):
    with LOCK:
        conf = _night_conf()
        if not conf:
            return JSONResponse({"error": "이 사건에는 밤이 없습니다"}, status_code=409)
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "그 배역으로 정할 수 없습니다"}, status_code=403)
        n = ROOM.setdefault("night", {"open": False, "picks": {}, "result": None})
        if not n.get("open"):
            return JSONResponse({"error": "지금은 밤이 아닙니다"}, status_code=409)
        opts = [o["id"] for o in ((conf.get("options") or {}).get(b.roleId) or [])]
        if b.optId not in opts:
            return JSONResponse({"error": "없는 선택지"}, status_code=404)
        n["picks"][b.roleId] = b.optId
        # 무엇을 골랐는지는 안 적는다. 누가 «정했다»까지만 알린다.
        nm = (SC.get_character(b.roleId) or {}).get("name", b.roleId)
        ROOM["table"].append({"kind": "system", "broadcast": True, "text": f"{nm} — 방에 불이 꺼졌습니다."})
        bump()
        _night_try_resolve()
        return {"ok": True, "night": _night_public(b.roleId)}


@app.post("/api/night/close")
def night_close(b: HostReq):
    with LOCK:
        if ROOM.get("host") not in (None, b.clientId) and not _agent_ok(b.key):
            return JSONResponse({"error": "host"}, status_code=403)
        _night_resolve()
        return {"ok": True, "night": _night_public()}



# ── 질문지 — 고갯짓 둘로만 답이 오는 막 ──────────────────────────
def _ask_conf():
    return getattr(SC, "ASK_SHEET", None)


def _ask_order() -> list:
    return [rid for rid in SC.__dict__.get("CHARACTERS", [])] if False else [
        c["id"] for c in SC.CHARACTERS
        if (ROOM["roles"].get(c["id"]) or {}).get("mode") in ("human", "ai")]


def _ask_open() -> None:
    conf = _ask_conf()
    if not conf:
        return
    a = ROOM.setdefault("ask", {"open": False, "asked": [], "turn": None})
    if a.get("open") or a.get("asked"):
        return
    order = _ask_order()
    a["open"] = True
    a["asked"] = []
    a["turn"] = order[0] if order else None
    ROOM["table"].append({"kind": "system", "broadcast": True,
                          "text": conf.get("notice", "질문지가 침대 발치에 놓였습니다 — 순서대로 하나씩 고르세요.")})
    _ev("ask", state="open")
    _ask_step()


def _ask_step() -> None:
    """차례를 넘긴다. AI 배역이면 그 자리에서 자기 몫을 고른다."""
    conf = _ask_conf()
    a = ROOM.get("ask") or {}
    if not conf or not a.get("open"):
        return
    order = _ask_order()
    done = {r["by"] for r in a["asked"]}
    left = [rid for rid in order if rid not in done]
    if not left:
        a["open"] = False
        a["turn"] = None
        ROOM["table"].append({"kind": "system", "broadcast": True,
                              "text": "— 질문지가 덮였습니다. 남은 것은 아무도 묻지 않았습니다."})
        _ev("ask", state="done")
        bump()
        return
    a["turn"] = left[0]
    if (ROOM["roles"].get(a["turn"]) or {}).get("mode") == "ai":
        rem = _ask_remaining()
        try:
            qid = SC.ask_ai_pick(a["turn"], rem)
        except Exception:                                # noqa: BLE001
            qid = rem[0] if rem else ""
        if qid:
            _ask_record(a["turn"], qid)
            return
    bump()


def _ask_remaining() -> list:
    conf = _ask_conf() or {}
    used = {r["q"] for r in (ROOM.get("ask") or {}).get("asked", [])}
    return [q["id"] for q in conf.get("questions", []) if q["id"] not in used]


def _ask_record(role_id: str, qid: str) -> None:
    conf = _ask_conf() or {}
    q = next((x for x in conf.get("questions", []) if x["id"] == qid), None)
    if not q:
        return
    a = ROOM["ask"]
    a["asked"].append({"by": role_id, "q": qid})
    nm = (SC.get_character(role_id) or {}).get("name", role_id)
    ans = conf.get("nod", "끄덕") if q["a"] == "nod" else conf.get("shake", "도리")
    ROOM["table"].append({"kind": "system", "broadcast": True,
                          "text": f'{nm} — 「{q["q"]}」\n    …{ans}.'})
    _ev("ask", by=role_id, q=qid, a=q["a"])
    _ask_step()


def _ask_public(role_id: str = "") -> dict | None:
    conf = _ask_conf()
    if not conf:
        return None
    a = ROOM.get("ask") or {}
    if not a.get("open") and not a.get("asked"):
        return None
    used = {r["q"]: r["by"] for r in a.get("asked", [])}
    qs = []
    for q in conf.get("questions", []):
        askedby = used.get(q["id"])
        qs.append({"id": q["id"], "q": q["q"], "askedBy": askedby or "",
                   "a": (q["a"] if askedby else "")})
    return {"open": bool(a.get("open")), "turn": a.get("turn") or "",
            "kick": conf.get("kick", ""), "title": conf.get("title", ""),
            "intro": conf.get("intro", ""), "prompt": conf.get("prompt", ""),
            "nod": conf.get("nod", "끄덕"), "shake": conf.get("shake", "도리"),
            "questions": qs, "asked": list(a.get("asked", [])),
            "total": len(_ask_order()), "done": not a.get("open") and bool(a.get("asked"))}


class AskPick(BaseModel):
    roleId: str
    clientId: str
    qid: str


@app.post("/api/ask")
def ask_pick(b: AskPick):
    with LOCK:
        if not _ask_conf():
            return JSONResponse({"error": "이 사건에는 질문지가 없습니다"}, status_code=409)
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "그 배역으로 물을 수 없습니다"}, status_code=403)
        a = ROOM.setdefault("ask", {"open": False, "asked": [], "turn": None})
        if not a.get("open"):
            return JSONResponse({"error": "질문지가 덮여 있습니다"}, status_code=409)
        if a.get("turn") != b.roleId:
            return JSONResponse({"error": "아직 당신 차례가 아닙니다"}, status_code=409)
        if b.qid not in _ask_remaining():
            return JSONResponse({"error": "이미 물어본 질문이거나 없는 질문입니다"}, status_code=409)
        _ask_record(b.roleId, b.qid)
        bump()
        return {"ok": True, "ask": _ask_public(b.roleId)}


# ── 중간 지목 — 판이 끝나기 전에 한 번 이름을 부른다 ─────────────────
def _tally(picks: dict) -> dict:
    out = {}
    for t in picks.values():
        out[t] = out.get(t, 0) + 1
    return out


def _person(rid: str) -> dict:
    """이름표 하나. 배역이든 NPC든 상관없이 찾아준다.

    소지품이 열릴 때 받아치는 사람은 대개 NPC다(토마스 렌·마시 선생). 배역 목록만
    뒤지면 그 자리에 id가 그대로 찍힌다."""
    for fn in ("get_character", "get_npc"):
        f = getattr(SC, fn, None)
        if not f:
            continue
        try:
            c = f(rid)
        except Exception:                      # noqa: BLE001 — 이름 하나 때문에 판이 멈추면 안 된다
            c = None
        if c:
            return c
    return {}


def _person_name(rid: str) -> str:
    return (_person(rid) or {}).get("name", rid)


def _belongings_public(who: str) -> dict | None:
    """소지품 — 내 것은 늘 보이고, 남의 것은 압수된 뒤에만 보인다.

    조사카드와 아예 다른 물건이라 손패·공개카드 어디에도 안 섞인다.
    압수 전에는 «누가 무엇을 쥐고 있다»조차 안 나간다 — 남의 주머니는 남의 것이다.
    """
    have = getattr(SC, "BELONGINGS", {}) or {}
    if not have:
        return None
    seized = list(ROOM.get("seized") or [])

    def pack(rid):
        b = have.get(rid) or {}
        ch = SC.get_character(rid) or {}
        return {"roleId": rid, "id": rid, "name": ch.get("name", rid), "job": ch.get("job", ""),
                "avatar": ch.get("avatar", ""), "color": ch.get("color", ""),
                "title": b.get("title", ""), "spot": b.get("spot", ""),
                "text": b.get("text", ""), "hint": b.get("hint", "")}

    return {"kick": getattr(SC, "BELONGINGS_KICK", "품 안에 있는 것"),
            "title": getattr(SC, "BELONGINGS_TITLE", "내 소지품"),
            "note": getattr(SC, "BELONGINGS_NOTE", ""),
            "seizedLabel": getattr(SC, "BELONGINGS_SEIZED_LABEL", "압수된 소지품"),
            "mine": pack(who) if who in have else None,
            "mineSeized": who in seized,
            "seized": [pack(r) for r in seized if r != who],
            "total": len(have), "seizedN": len(seized)}


def _seize_belongings() -> list:
    """1차 지목의 최다 득표자에게서 소지품을 압수한다.

    표가 갈리면 갈린 사람 전부다 — 「제일 많이 받은 사람」이 여럿이면 여럿이 내놓는다.
    소지품이 없는 이름(NPC·당주)은 셈에는 들어가도 압수할 것이 없으니 빠진다.
    한 번 압수된 것은 되돌리지 않는다.

    열린 물건마다 자리에 있던 사람들이 한마디씩 한다. 그 말은 BELONGINGS_ORDER
    차례대로 나간다 — 둘이 한꺼번에 열렸을 때 반응이 서로 물리면 누가 무엇에 대고
    하는 말인지 사라진다. 각 줄에는 drip 표를 붙여 보낸다. 클라이언트가 그 표를 보고
    한 줄씩 띄운다(0.5초). 한 덩어리로 솟으면 대화가 아니라 공지처럼 읽힌다.
    """
    have = getattr(SC, "BELONGINGS", {}) or {}
    if not have:
        return ROOM.get("seized") or []
    picks = ((ROOM.get("accuse1") or {}).get("picks") or {})
    tally: dict[str, int] = {}
    for t in picks.values():
        tally[t] = tally.get(t, 0) + 1
    if not tally:
        return ROOM.get("seized") or []
    top = max(tally.values())
    order = list(getattr(SC, "BELONGINGS_ORDER", []) or []) or sorted(have)
    rank = {r: i for i, r in enumerate(order)}
    lead = sorted((t for t, v in tally.items() if v == top and t in have),
                  key=lambda r: (rank.get(r, len(rank)), r))
    cur = ROOM.setdefault("seized", [])
    new = [r for r in lead if r not in cur]
    if not new:
        return cur
    cur.extend(new)
    names = " · ".join(_person_name(r) for r in new)
    ROOM["table"].append({"kind": "system", "broadcast": True,
                          "text": f"— 표가 가장 많이 모인 자리에서 소지품을 내놓게 했습니다 — {names}.\n"
                                  "    압수된 물건은 「내 정보 · 추가 정보」에서 모두가 봅니다."})
    for rid in new:
        b = have.get(rid) or {}
        ti = b.get("title", "")
        ROOM["table"].append({"kind": "system", "broadcast": True, "drip": True,
                              "text": f"— {_person_name(rid)}의 {b.get('spot', '품')}에서 "
                                      f"「{ti}」{_subj(ti)} 나왔습니다."})
        for ln in b.get("react") or []:
            say = (ln.get("text") or "").strip()
            if not say:
                continue
            who = ln.get("who") or ""
            if not who:                         # 말이 아니라 그 자리에서 벌어진 일
                ROOM["table"].append({"kind": "system", "broadcast": True,
                                      "drip": True, "text": f"    {say}"})
                continue
            ROOM["table"].append({"kind": "ai", "roleId": who, "speaker": _person_name(who),
                                  "text": say, "auto": True, "drip": True})
    return cur


def _accuse1_public() -> dict | None:
    """그 막에서 던진 표. 판정은 안 한다 — 이 표는 종막까지 그대로 따라간다."""
    ph = SC.phase_by_seq(ROOM["seq"])
    a1 = ROOM.get("accuse1") or {"seq": None, "picks": {}}
    if ph.get("key") != "accuse" and not a1.get("picks"):
        return None
    humans = _human_roles()
    picks = dict(a1.get("picks") or {})
    tally = {}
    for t in picks.values():
        tally[t] = tally.get(t, 0) + 1
    top = max(tally.values()) if tally else 0
    lead = sorted([t for t, v in tally.items() if v == top]) if top else []
    # 비밀투표다. 다 던지기 전에는 누가 누구를 적었는지도, 몇 표인지도 안 나간다 —
    # 표를 세어가며 눈치껏 얹는 판이 되면 「동시에 편다」가 아무 뜻이 없다.
    # 봉인된 표다. 그 막에서는 결과도 안 연다 — 둘이 하는 판에서 표가 그 자리에서
    # 열리면 누가 누구를 적었는지가 그냥 드러난다. 봉투는 종막에서 뜯는다.
    done = bool(humans) and all(r in picks for r in humans)
    open_ = ph.get("key") == "accuse"
    shown = ph.get("key") in ("final", "reveal")
    out = {"open": open_, "shown": shown, "voters": len(humans), "voted": len(picks),
           "done": done, "mineDone": False, "picks": {}, "tally": {}, "lead": [], "tie": False}
    if shown:
        out.update({"picks": picks, "tally": tally, "lead": lead, "tie": len(lead) > 1})
    return out


@app.post("/api/accuse1")
def accuse_interim(b: VoteReq):
    """중간 지목. 종막의 /api/accuse 와 달리 판정이 없고, 표는 지워지지 않는다."""
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "그 배역으로 지목할 수 없습니다"}, status_code=403)
        if SC.phase_by_seq(ROOM["seq"]).get("key") != "accuse":
            return JSONResponse({"error": "지목 페이즈에서만 할 수 있습니다"}, status_code=409)
        # 이 집 사람이면 누구든 적을 수 있다 — 배역도, 앉을 수 없는 자리도, 당주 본인도.
        # 자기 이름도 막지 않는다. 그렇게 적을 이유가 있는 판이다.
        ok = set(ROOM["roles"]) | {n["id"] for n in (getattr(SC, "NPCS", []) or [])} | {"victim"}
        if b.targetRoleId not in ok:
            return JSONResponse({"error": "이 집 사람이 아닙니다"}, status_code=404)
        a1 = ROOM.setdefault("accuse1", {"seq": None, "picks": {}})
        first = b.roleId not in a1["picks"]
        a1["seq"] = ROOM["seq"]
        a1["picks"][b.roleId] = b.targetRoleId
        nm = (SC.get_character(b.roleId) or {}).get("name", b.roleId)
        # 무엇을 적었는지도, 결과도 그 자리에서는 안 연다. 종막에서 봉투를 뜯는다.
        if first and all(r in a1["picks"] for r in _human_roles()):
            # 소지품을 쓰는 사건에서는 이 표가 종막까지 잠들어 있지 않다 — 여기서 한 번 값을 한다.
            tail = ("    표를 가장 많이 받은 사람은 그 자리에서 소지품을 압수당합니다."
                    if getattr(SC, "BELONGINGS", None) else "    이 표는 종막에서 열립니다.")
            with _drip():
                ROOM["table"].append({"kind": "system", "broadcast": True,
                                      "text": "— 모두 적었습니다. 종이는 접힌 채로 봉투에 들어갔습니다.\n" + tail})
                # 누구를 적었는지는 안 열어도, 제일 많이 불린 이름은 그 자리에서 값을 치른다.
                _seize_belongings()
        bump()
    return {"ok": True, "accuse1": _accuse1_public()}


@app.post("/api/interrogate/vote")
def interrogate_vote(b: InterrogateVote):
    """토론 페이즈 추가 심문 1회 신청 — 과반수(2인이면 만장일치) 찬성 시 부여된다."""
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "그 배역으로 투표할 수 없습니다"}, status_code=403)
        ph = SC.phase_by_seq(ROOM["seq"])
        if ph.get("key") != "talk":
            return JSONResponse({"error": "토론 페이즈에서만 신청할 수 있습니다"}, status_code=409)
        budget = _interrogate_budget()
        if budget["bonusGranted"]:
            return {"ok": True, "budget": budget}
        votes = ROOM["interrogate"]["votes"]
        if b.roleId not in votes:
            votes.append(b.roleId)
        budget = _interrogate_budget()
        if budget["voteNeed"] > 0 and len(votes) >= budget["voteNeed"]:
            ROOM["interrogate"]["bonus"] = True
            ROOM["table"].append({"kind": "system", "broadcast": True,
                                  "text": "추가 심문 1회가 승인됐습니다."})
        bump()
        return {"ok": True, "budget": _interrogate_budget()}


@app.post("/api/interrogate")
def interrogate(b: Interrogate):
    """심층심문 — 상대가 지금 손패로 쥔 카드를 지목해 답을 요구한다. 카드 자체는 공개되지
    않는다 — 대답만 들을 뿐이고, 민감한 카드는 그 대답이 진실이 아닐 수도 있다(캐릭터가
    거짓/얼버무림으로 넘어간다). 어떤 증거를 대는지가 곧 추리다 — 증거 없이 물으면 기본
    확률, 엉뚱한 카드를 대면 오히려 확률이 떨어지고(허 찔러 넘어갈 여지를 준다), 시나리오가
    정한 정확한 카드를 짚으면 반드시 실토한다. 예산(1회)은 성패와 무관하게 소모된다."""
    with LOCK:
        r = ROOM["roles"].get(b.askerRoleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "그 배역으로 심문할 수 없습니다"}, status_code=403)
        ph = SC.phase_by_seq(ROOM["seq"])
        if ph.get("key") != "talk":
            return JSONResponse({"error": "심층심문은 토론 페이즈에서만 할 수 있습니다"}, status_code=409)
        if b.askerRoleId == b.targetRoleId:
            return JSONResponse({"error": "자기 자신은 심문할 수 없습니다"}, status_code=409)
        if b.targetRoleId not in ROOM["roles"]:
            return JSONResponse({"error": "없는 배역"}, status_code=404)
        # 사람이 맡은 배역은 심층심문의 대상이 아니다.
        # 이 기능은 «AI 가 연기하는 배역에게서 대답을 끌어내는» 장치다 —
        # 사람끼리는 그냥 대화창에서 물으면 되고, 그 자리에서 무엇을 말할지는
        # 판정이 아니라 그 사람이 정할 몫이다.
        if (ROOM["roles"][b.targetRoleId] or {}).get("mode") == "human":
            return JSONResponse({"error": "사람이 맡은 배역은 심층심문할 수 없습니다 — 직접 물어보세요"},
                                status_code=409)
        budget = _interrogate_budget()
        if budget["remaining"] <= 0:
            return JSONResponse({"error": "이번 토론에서 쓸 수 있는 심문 횟수를 다 썼습니다"}, status_code=409)
        selfmode = not (b.cardId or "").strip()
        if not selfmode and b.cardId not in ROOM["hands"].get(b.targetRoleId, []):
            return JSONResponse({"error": "그 배역이 지금 들고 있는 카드가 아닙니다"}, status_code=409)
        # 사건에 따라 한 카드는 한 번만 추궁에 쓸 수 있다. 같은 자리를 몇 번이고
        # 들이대며 갈아 마시는 판을 막는다 — 무엇을 물을지 고르는 것이 곧 심문이다.
        once = bool(getattr(SC, "INTERROGATE_ONCE", False))
        used = ROOM.setdefault("itgUsed", [])
        if once and not selfmode and b.cardId in used:
            c0 = SC.get_card(b.cardId) or {}
            return JSONResponse({"error": f'「{c0.get("title", "그 카드")}」는 이미 추궁에 쓴 카드입니다 — 다른 것으로 물으세요'},
                                status_code=409)

        evid_id = (b.evidenceCardId or "").strip()
        if evid_id:
            has_access = evid_id in ROOM["hands"].get(b.askerRoleId, []) or evid_id in ROOM["revealed"]
            if not has_access:
                return JSONResponse({"error": "내가 갖고 있지 않은 카드는 증거로 댈 수 없습니다"}, status_code=409)
            if once and evid_id in used:
                c0 = SC.get_card(evid_id) or {}
                return JSONResponse({"error": f'「{c0.get("title", "그 카드")}」는 이미 추궁에 쓴 카드입니다'},
                                    status_code=409)

        card = None if selfmode else SC.get_card(b.cardId)
        target = SC.get_character(b.targetRoleId) or {}
        asker = SC.get_character(b.askerRoleId) or {}
        # 사람을 찌를 때는 그 배역의 «정체» 항목을 쓴다. 실토해도 한 겹만 벗겨진다.
        entry = ((getattr(SC, "INTERROGATE_SELF", {}) or {}).get(b.targetRoleId) if selfmode
                 else (getattr(SC, "INTERROGATE", {}) or {}).get(b.targetRoleId, {}).get(b.cardId))
        # 사건이 정체 답변을 한 줄로만 적어둔 경우가 있다. 그대로 두면 아래에서
        # 문자열을 사전처럼 다루다 500 이 나고, 화면에는 「network」만 뜬다.
        if isinstance(entry, str):
            entry = {"evasive": entry, "truth": entry, "rebuttal": ""}

        # 같은 자리를 몇 번 찔렸는가. 한 번 얼버무렸다고 그 정보가 닫히면 안 된다 —
        # 얼버무림은 「여기가 아프다」는 신호이고, 계속 밀면 결국 분다.
        pk = f'{b.targetRoleId}:{b.cardId or "_self"}'
        pressed = ROOM.setdefault("press", {}).get(pk, 0)

        # 들이민 증거가 이 사람을 가리키는 카드라면, 실토는 그 증거에 얽힌 진상이 된다.
        # 「무엇을 묻는가」보다 「무엇을 들이미는가」가 답을 정한다.
        ev_entry = None
        # ── 심문은 확률이 아니라 태그로 굴러간다 ──────────────────────────
        # 카드마다 「이 카드는 누구의 비밀인가」가 INTERROGATE 표에 적혀 있다.
        #  · 그 사람이 그 카드를 쥐고 있고 그 자리를 지목했다  → 얼버무리고 그 자리에서 분다
        #  · 내가 그 카드를 쥐고 그 사람에게 들이밀었다        → 마찬가지
        #  · 그 사람과 상관없는 카드를 들이밀었다              → 정말로 모른다. 아는 척도 안 한다
        #  · 그 사람이 쥔 카드인데 그의 비밀이 아니다          → 그냥 내용을 사실대로 말한다
        # 예전엔 아무 카드나 대도 실토가 나왔고(«무엇을 들이미는가»가 무의미했다),
        # 그 전에는 주사위가 답을 정했다(같은 추리가 판마다 다르게 끝났다).
        ev_entry = None
        stumped = False        # 상관없는 카드를 들이밀었나
        if evid_id and not selfmode:
            if b.targetRoleId in _points_at(evid_id):
                ev_entry = (getattr(SC, "INTERROGATE", {}) or {}).get(b.targetRoleId, {}).get(evid_id)
            if not ev_entry:
                stumped = True
        ev_hit = bool(ev_entry)
        if ev_entry:
            entry = ev_entry
            pk = f'{b.targetRoleId}:{evid_id}'
            pressed = ROOM.setdefault("press", {}).get(pk, 0)
        # 정체를 찌를 때는 그 사람의 «정체를 벗기는 카드» 한 장만 통한다.
        elif selfmode and entry and evid_id and evid_id == (entry.get("rebuttal") or ""):
            ev_hit = True

        if stumped:
            # 남의 비밀이 든 카드를 엉뚱한 사람에게 들이민 것이다. 아는 척할 이유가 없다.
            shown = SC.get_card(evid_id) or {}
            outcome = "unknown"
            line = f'"「{shown.get("title", "그거")}」요? …그건 제가 아는 게 없습니다. 정말이에요."'
        elif entry and not selfmode:
            # 그의 비밀이 든 카드다. 한 번 얼버무렸다가 같은 턴에 분다 —
            # 아픈 데를 정확히 짚어놓고 턴을 한 번 더 쓰게 만들 이유가 없다.
            ROOM["press"].pop(pk, None)
            ev = entry["evasive"]
            first = ev[0] if isinstance(ev, (list, tuple)) else ev
            outcome = "truth"
            line = f'{first}\n\n…{entry["truth"]}'
        elif entry:
            # 정체 추궁 — 벗기는 카드를 들이밀지 않는 한 넘어간다. 계속 밀면 결국 분다.
            ev = entry["evasive"]
            first = ev[0] if isinstance(ev, (list, tuple)) else ev
            if ev_hit or pressed >= INTERROGATE_PRESS_BREAK:
                ROOM["press"].pop(pk, None)
                outcome = "truth"
                line = f'{first}\n\n…{entry["truth"]}' if ev_hit else entry["truth"]
            else:
                ROOM["press"][pk] = pressed + 1
                outcome = "evasive"
                line = ev[min(pressed, len(ev) - 1)] if isinstance(ev, (list, tuple)) else ev
        elif selfmode:
            outcome, line = "evasive", "\"…무슨 말씀이신지 모르겠습니다.\""
        else:
            intro = (getattr(SC, "INTERROGATE_PLAIN", {}) or {}).get(b.targetRoleId, "")
            outcome, line = "plain", f'{intro} 「{card["title"]}」— {card["text"]}'.strip()

        ROOM["interrogate"]["used"] += 1
        if bool(getattr(SC, "INTERROGATE_ONCE", False)):
            for cid in (b.cardId, evid_id):
                if cid and cid not in ROOM["itgUsed"]:
                    ROOM["itgUsed"].append(cid)

        badge = {"truth": "실토", "evasive": "얼버무림", "plain": "답변", "unknown": "모른다"}[outcome]
        if outcome == "evasive":
            nxt = ROOM["press"].get(pk, 0)
            if nxt >= INTERROGATE_PRESS_BREAK:
                badge = "얼버무림 · 더는 못 버틴다"
            elif nxt >= 2:
                badge = "말이 흔들린다"
        elif outcome == "truth" and pressed:
            badge = "실토 — 끝내 무너졌다"
        if selfmode:
            header = f'{badge} — {asker.get("name","")} → {target.get("name","")} · 본인 추궁'
        else:
            # 증거가 답을 정했으면 머리글도 그 카드를 가리켜야 한다 — 아니면 엉뚱한 카드에
            # 대해 실토한 것처럼 읽힌다.
            shown = (SC.get_card(evid_id) if (ev_entry or stumped) else None) or card
            where = f'{shown["locName"]} · {shown["spot"]}' if shown.get("spot") else shown["locName"]
            header = f'{badge} — {asker.get("name","")} → {target.get("name","")} · [{where}] 「{shown["title"]}」'
        # 대화창에서는 「추궁 결과」 상자가 아니라 그 사람이 대답하는 말로 선다 —
        # 카드를 내려놓으며 말하는 그림이라야 추리가 대화로 읽힌다. roleId/speaker 는
        # 답하는 쪽(추궁당한 사람)이고, 옆에 놓이는 카드는 방금 그 자리에서 오간 카드다.
        talked = shown if not selfmode else None
        ROOM["table"].append({"kind": "interrogate", "broadcast": True,
                              "askerRoleId": b.askerRoleId, "targetRoleId": b.targetRoleId,
                              "roleId": b.targetRoleId, "speaker": target.get("name", ""),
                              "askerName": asker.get("name", ""), "badge": badge,
                              "showCardId": (talked or {}).get("id", ""),
                              "showCardTitle": (talked or {}).get("title", ""),
                              "cardId": b.cardId, "outcome": outcome, "text": header, "line": line})
        bump()
        return {"ok": True, "outcome": outcome, "line": line,
                "press": ROOM["press"].get(pk, 0), "budget": _interrogate_budget()}


@app.get("/api/hand/{role_id}")
def get_hand(role_id: str, clientId: str = ""):
    with LOCK:
        r = ROOM["roles"].get(role_id)
        if not r or r["clientId"] != clientId:  # 엄격: 내 손패만
            return JSONResponse({"error": "권한 없음"}, status_code=403)
        mine = ROOM["hands"].get(role_id, [])
        return {"hand": [SC.public_card(c) for c in mine],
                "notes": _my_notes(role_id, mine)}


# ── 에이전트(코드 세션) 원격 조종: GM 읽기 + AI 배역 대리 행동 ──
@app.get("/api/gm")
def gm(key: str = ""):
    if not _agent_ok(key):
        return JSONResponse({"error": "key"}, status_code=403)
    with LOCK:
        return {
            "seq": ROOM["seq"], "round": current_round(ROOM["seq"]),
            "phase": SC.phase_by_seq(ROOM["seq"]),
            "roles": {rid: {"mode": r["mode"], "claimed": r["clientId"] is not None} for rid, r in ROOM["roles"].items()},
            "table": ROOM["table"],
            "revealed": [SC.public_card(c) for c in ROOM["revealed"]],
            "hands": {rid: [SC.public_card(c) for c in cs] for rid, cs in ROOM["hands"].items()},
            "grades": ROOM["grades"],
            "finalAnswers": ROOM["finalAnswers"],
        }


@app.post("/api/agent/say")
def agent_say(b: AgentSay):
    if not _agent_ok(b.key):
        return JSONResponse({"error": "key"}, status_code=403)
    with LOCK:
        c = SC.get_character(b.roleId)
        if not c:
            return JSONResponse({"error": "없는 배역"}, status_code=404)
        ROOM["table"].append({"kind": "ai", "roleId": b.roleId, "speaker": c["name"], "text": b.text.strip()})
        bump()
    return {"ok": True}


@app.post("/api/agent/investigate")
def agent_investigate(b: AgentCard):
    if not _agent_ok(b.key):
        return JSONResponse({"error": "key"}, status_code=403)
    with LOCK:
        err = _try_investigate(b.roleId, b.cardId)
        if err:
            return JSONResponse({"error": err}, status_code=409)
    return {"card": SC.public_card(b.cardId)}


@app.post("/api/turn/next")
def turn_next(b: TurnReq):
    """다음 조사 차례로 넘기기 — 호스트/GM, 또는 현재 차례 당사자. 호스트 미설정 시 통과."""
    with LOCK:
        role = ROOM["roles"].get(b.roleId) or {}
        allowed = _agent_ok(b.key) or (b.roleId and b.roleId == ROOM.get("turn") and role.get("clientId") == b.clientId)
        if ROOM.get("host") is not None:
            allowed = allowed or _is_host(b.clientId)
        else:
            allowed = True
        if not allowed:
            return JSONResponse({"error": "권한 없음"}, status_code=403)
        if _crisis_blocking():
            return JSONResponse({"error": "침수 대응이 먼저입니다"}, status_code=409)
        _advance_turn()
        return {"ok": True, "turn": ROOM.get("turn")}


@app.post("/api/ai-investigate")
def ai_investigate_auto(b: TurnReq):
    """AI 배역 자동 조사(휴리스틱, 즉시). 대상 미지정 시 현재 차례 배역."""
    with LOCK:
        rid = b.roleId or ROOM.get("turn")
        if not rid or rid not in ROOM["roles"]:
            return JSONResponse({"error": "대상 배역 없음"}, status_code=400)
        allowed = _agent_ok(b.key) or (ROOM.get("host") is None) or _is_host(b.clientId)
        if not allowed:
            return JSONResponse({"error": "권한 없음"}, status_code=403)
        if _ap_for(ROOM["seq"]) <= 0:
            return JSONResponse({"error": "조사 페이즈가 아닙니다"}, status_code=409)
        picks = _ai_take_turn(rid)
        cat = {c["id"]: c for c in SC.CARDS}
    return {"ok": True, "roleId": rid,
            "picked": [{"id": i, "title": cat[i]["title"], "loc": cat[i]["loc"], "locName": cat[i]["locName"]} for i in picks]}


@app.get("/api/events")
def events(key: str = "", since: int = 0, wait: int = 0):
    """진행 세션이 따라 읽는 사건 목록. since 이후 것만 준다.

    세션은 푸시를 못 받으니 스스로 물어봐야 한다. wait를 주면 새 사건이 생길 때까지
    그만큼(최대 25초) 붙들고 있다가 답한다 — 짧은 간격으로 되묻지 않아도 되도록.
    새 게 없으면 빈 목록으로 돌아온다.
    """
    if not _agent_ok(key):
        return JSONResponse({"error": "key"}, status_code=403)
    deadline = time.monotonic() + max(0, min(25, wait))
    while True:
        with LOCK:
            evs = [e for e in ROOM.get("events", []) if e["id"] > since]
            all_ev = ROOM.get("events") or []
            cursor = all_ev[-1]["id"] if all_ev else 0
            ph = SC.phase_by_seq(ROOM["seq"])
            turn = ROOM.get("turn")
        if evs or time.monotonic() >= deadline:
            return {"cursor": cursor, "events": evs, "turn": turn,
                    "phase": {"seq": ph["seq"], "name": ph["name"],
                              "key": ph.get("key", ""), "ap": int(ph.get("ap", 0) or 0)}}
        time.sleep(0.6)


@app.get("/api/relay-next")
def relay_next(key: str = "", clientId: str = ""):
    """지금 말할 차례인 AI 배역만 알려준다. 내용은 주지 않는다.

    진행 세션은 이 이름 하나만 받아서 그 배역의 서브에이전트를 띄우면 된다.
    카드도 대본도 진행 세션을 지나가지 않는다.
    """
    if not (_agent_ok(key) or _is_host(clientId)):
        return JSONResponse({"error": "key"}, status_code=403)
    rid = _pick_reactor()
    if not rid:
        return JSONResponse({"error": "AI 배역이 없습니다"}, status_code=409)
    c = SC.get_character(rid) or {}
    return {"roleId": rid, "name": c.get("name", rid)}


@app.get("/api/relay/{role_id}", response_class=PlainTextResponse)
def relay_prompt(role_id: str, key: str = "", clientId: str = ""):
    """API 키가 없을 때 쓰는 통로 — 그 배역 하나짜리 지시문을 글로 내준다.

    보통 채팅 세션에는 임의의 주소로 요청을 보낼 손이 없다. 그래서 서버가 대신
    부를 수 없고, 사람이 이 글을 복사해 채팅에 넣고 돌아온 대사를 도로 붙여넣는다.
    ANTHROPIC_API_KEY를 넣으면 서버가 직접 부르므로 이 통로는 필요 없어진다.

    담기는 것은 공개 카드와 '그 배역 자신의 손패'뿐이다. 남의 손패는 들어가지 않는다.
    """
    if not (_agent_ok(key) or _is_host(clientId)):
        return PlainTextResponse("key", status_code=403)
    if not SC.get_character(role_id):
        return PlainTextResponse("없는 배역", status_code=404)
    with LOCK:
        r = ROOM["roles"].get(role_id) or {}
    if r.get("mode") != "ai":
        return PlainTextResponse("사람이 맡은 배역입니다", status_code=409)
    return _role_prompt(role_id, _pick_nudge(role_id))


@app.get("/relay")
def relay_page():
    return FileResponse(_HERE / "relay.html")


@app.get("/api/handoff", response_class=PlainTextResponse)
def handoff_brief(key: str = "", base: str = ""):
    """진행 세션이 스스로 받아 가는 지침. 배포된 코드에서 만들어지므로 낡을 일이 없다.

    저장소를 진행 세션에 주지 않기로 한 이상, 지침을 사람이 복사해 나르면 판마다
    조금씩 어긋난다. 이 주소 하나만 알려주면 그 문제가 없어진다.
    진상은 들어 있지 않다.
    """
    if not _agent_ok(key):
        return PlainTextResponse("key", status_code=403)
    # 배포에 LLM 키가 있느냐에 따라 지침이 갈린다 — 없는데 ai-react를 알려주면 502만 만난다
    has_llm = bool(ANTHROPIC_API_KEY and anthropic) or BACKEND == "ollama"
    return handoff.runner_brief(SC, base or "", key or "<AGENT_KEY>", has_llm=has_llm)


@app.get("/api/player-notice", response_class=PlainTextResponse)
def player_notice(base: str = ""):
    """플레이어에게 뿌릴 안내문. 비밀이 없으므로 키를 걸지 않는다."""
    return handoff.player_notice(SC, base or "")


@app.get("/handoff")
def handoff_page():
    return FileResponse(_HERE / "handoff.html")


@app.get("/api/brief")
def brief(key: str = ""):
    """세션 에이전트(코워크 GM)용 브리핑 — 각 배역 손패의 '내용 포함' + 공개 카드. GM 전용."""
    if not _agent_ok(key):
        return JSONResponse({"error": "key"}, status_code=403)
    with LOCK:
        cat = {c["id"]: c for c in SC.CARDS}
        # 손패는 '몇 장 들었나'까지만. 남이 조사한 카드의 내용은 진행 세션도 보지 않는다 —
        # 진행자가 그걸 다 보고 있으면 판이 끝나기 전에 답을 짚어낼 수 있고,
        # 그러면 '알아도 말하지 않는다'는 약속에 기대야 한다. 안 보는 게 낫다.
        hand_counts = {rid: len(ids) for rid, ids in ROOM["hands"].items() if ids}
        revealed = [{"id": i, "title": cat[i]["title"], "locName": cat[i]["locName"],
                     "text": cat[i].get("text", ""), "hint": cat[i].get("hint", "")}
                    for i in ROOM["revealed"] if i in cat]
        ph = SC.phase_by_seq(ROOM["seq"])
        return {"phase": ph["name"], "round": current_round(ROOM["seq"]), "turn": ROOM.get("turn"),
                "turnOrder": _turn_order(), "handCounts": hand_counts, "revealed": revealed}


@app.post("/api/agent/reveal")
def agent_reveal(b: AgentCard):
    if not _agent_ok(b.key):
        return JSONResponse({"error": "key"}, status_code=403)
    with LOCK:
        _publish(b.cardId)
    return {"ok": True}


@app.post("/api/agent/advance")
def agent_advance(b: KeyOnly):
    if not _agent_ok(b.key):
        return JSONResponse({"error": "key"}, status_code=403)
    return _advance()


@app.post("/api/agent/narrate")
def agent_narrate(b: AgentSay):  # roleId 무시, text=GM 내레이션(전체 방송)
    if not _agent_ok(b.key):
        return JSONResponse({"error": "key"}, status_code=403)
    with LOCK:
        ROOM["table"].append({"kind": "system", "broadcast": True, "text": b.text.strip()})
        bump()
    return {"ok": True}


@app.post("/api/crisis")
def crisis_answer(b: CrisisAnswer):
    """배역 하나의 판단을 접수한다. 남의 배역으로는 낼 수 없다."""
    with LOCK:
        cr = ROOM.get("crisis") or {}
        if not cr.get("open"):
            return JSONResponse({"error": "지금은 답할 때가 아닙니다"}, status_code=409)
        r = ROOM["roles"].get(b.roleId)
        if not r:
            return JSONResponse({"error": "없는 배역"}, status_code=404)
        if r["clientId"] and r["clientId"] != b.clientId and not _agent_ok(b.key):
            return JSONResponse({"error": "본인 배역만 답할 수 있습니다"}, status_code=403)
        n = len(SC.CRISIS["questions"])
        if len(b.answers) != n or any(not isinstance(x, int) for x in b.answers):
            return JSONResponse({"error": "세 문항을 모두 고르세요"}, status_code=400)
        cr["answers"][b.roleId] = list(b.answers)
        _crisis_try_resolve()
        bump()
    return {"ok": True}


@app.post("/api/crisis/close")
def crisis_close(b: HostReq):
    """기다리다 말고 지금 판정한다 — 답 안 낸 배역은 틀린 것으로 친다."""
    if ROOM.get("host") is not None and not (_is_host(b.clientId) or _agent_ok(b.key)):
        return JSONResponse({"error": "host"}, status_code=403)
    with LOCK:
        _crisis_resolve()
        ROOM["flood"] = _flood_for(ROOM["seq"])
        bump()
    return {"ok": True, "solved": (ROOM.get("crisis") or {}).get("solved")}


@app.post("/api/advance")
def advance(b: HostReq):
    # 호스트가 지정돼 있으면 호스트나 GM 진행석만, 없으면 누구나(현행 앱 호환)
    if ROOM.get("host") is not None and not (_is_host(b.clientId) or _agent_ok(b.key)):
        return JSONResponse({"error": "host"}, status_code=403)
    return _advance()


def _seed_phase_lines(seq: int) -> None:
    """이 막에서 각자가 꺼내기로 한 말을 대화창에 그대로 올린다.
    예전에는 «내 정보 · 지금 할 말»에 조용히 붙어 있었다 — 열어보지 않으면 그 밤이
    그냥 지나갔다. 말은 말이 오가는 자리에 있어야 한다."""
    if not hasattr(SC, "memory_up_to"):
        return
    ph = SC.phase_by_seq(seq)
    when = ph.get("name", "")
    solved = (ROOM.get("crisis") or {}).get("solved")
    for c in SC.CHARACTERS:
        rid = c["id"]
        if (ROOM["roles"].get(rid) or {}).get("mode") not in ("human", "ai"):
            continue
        try:
            frags = SC.memory_up_to(rid, seq, solved) if solved is not None else SC.memory_up_to(rid, seq)
        except TypeError:
            frags = SC.memory_up_to(rid, seq)
        for f in frags:
            if f.get("when") == when and f.get("text"):
                ROOM["table"].append({"kind": "ai", "roleId": rid, "speaker": c["name"],
                                      "text": f["text"], "auto": True, "seq": seq})


def _seed_npc_lines(seq: int) -> None:
    """배역이 아닌 사람도 말은 한다.

    《자명종》의 마부가 그렇다. 그는 결백하고, 결백한 사람은 숨길 게 없다 —
    그래서 자기가 본 것을 그냥 대화창에 쏟는다. 다만 관심이 사건에 없어서
    말하는 김에 자기 돈 이야기부터 한다. 아무도 그 말을 진지하게 안 듣는다.

    공지가 아니라 «말»이므로 대화 말풍선으로 들어간다. 배역이 받아치는 자리도
    있어서, NPC 목록에 없으면 배역에서 찾는다.

    NPC_LINES 가 없는 사건에서는 아무 일도 안 일어난다.
    """
    for ln in (getattr(SC, "NPC_LINES", {}) or {}).get(seq, []) or []:
        who = ln.get("who", "")
        n = ((SC.get_npc(who) if hasattr(SC, "get_npc") else None)
             or (SC.get_character(who) if hasattr(SC, "get_character") else None) or {})
        say = (ln.get("say") or "").strip()
        if not say:
            continue
        ROOM["table"].append({"kind": "ai", "roleId": who, "speaker": n.get("name", who),
                              "text": say, "auto": True, "seq": seq})


def _advance():
    _ev("phase_leaving", name=SC.phase_by_seq(ROOM["seq"])["name"])
    with LOCK:
        ROOM["ready"] = []          # 준비 표시는 막마다 새로 받는다
        # 막이 넘어갈 때 붙는 줄은 전부 판이 스스로 하는 말이다 — 밤의 결과, 압수,
        # 막 머리, GM, NPC가 꺼내는 말. 한 덩어리로 솟지 않게 표를 달아 내보낸다.
        _drip0 = len(ROOM["table"])
        # 밤을 안 닫고 넘어가면 «아침이 안 온다». 밤 결과가 없으면 그날 밤의 자국도
        # 안 생겨서 마지막 조사에 열 카드가 모자란다 — 조사턴이 통째로 빈다.
        # 아직 안 고른 사람은 「안 갔다」로 본다. 그게 안 고른 것의 뜻이다.
        if (ROOM.get("night") or {}).get("open"):
            _night_resolve()
        # 지목 막을 떠나면 압수는 그 자리에서 끝난다 — 한 사람이 안 적고 넘어가도
        # 이미 모인 표로 셈한다. 안 그러면 소지품이 영영 안 열린 채로 판이 끝난다.
        if SC.phase_by_seq(ROOM["seq"]).get("key") == "accuse":
            _seize_belongings()
        if ROOM["seq"] < len(SC.PHASES):
            ROOM["seq"] += 1
            seq = ROOM["seq"]
            ph = SC.phase_by_seq(seq)
            il = SC.interlude_for(seq)
            if il:
                ROOM["table"].append({"kind": "system", "broadcast": True,
                                      "text": f"{getattr(SC, 'PA_LABEL', '교내방송')} — {il}"})
            ROOM["table"].append({"kind": "system", "text": f'— {ph["name"]} —'})
            # GM의 말은 대화창에 남는다. 머리띠에 한 줄로 띄워두면 다음 막에서 사라져
            # 놓친 사람이 다시 볼 데가 없었다 — 기록이 남는 자리는 여기뿐이다.
            if ph.get("gm"):
                ROOM["table"].append({"kind": "gm", "broadcast": True, "text": ph["gm"]})
            _seed_phase_lines(seq)
            _seed_npc_lines(seq)
            # 막이 바뀔 때 틀 컷. 없는 사건에서는 아무 일도 안 일어난다.
            _fire_cut(f"phase:{seq}")
            _ev("phase", name=ph["name"], key=ph.get("key", ""), min=ph.get("min", 0),
                ap=int(ph.get("ap", 0) or 0), gm=ph.get("gm", ""), interlude=il or "")
            _reset_turn_for_seq(seq)   # 조사 페이즈면 순번 초기화
            _auto_reveal_obligatory()
            # 막이 스스로 테이블에 올리는 카드. 조사턴으로는 못 여는 자리에 있던 것이
            # 이야기 진행에 맞춰 나오는 경우다 — 없는 사건에서는 아무 일도 안 일어난다.
            for cid in (getattr(SC, "PHASE_REVEAL", {}) or {}).get(seq, []) or []:
                if cid not in ROOM["revealed"]:
                    _publish(cid)
                    c = SC.get_card(cid) or {}
                    where = f'{c.get("locName","")} · {c.get("spot","")}' if c.get("spot") else c.get("locName", "")
                    ROOM["table"].append({"kind": "system", "broadcast": True,
                                          "text": f'[{where}] 「{c.get("title","")}」이(가) 테이블에 올랐습니다.'})
            conf = _crisis_conf()
            if conf and seq == conf["seq"]:
                _crisis_open()
            if ph.get("key") == "night":
                _night_open()
            if ph.get("key") == "ask":
                _ask_open()
            ROOM["flood"] = _flood_for(seq)
            _drip_from(_drip0)
            bump()
        else:
            _drip_from(_drip0)
        return {"seq": ROOM["seq"]}


class MentionCard(BaseModel):
    cardId: str
    roleId: str
    clientId: str
    text: str = ""          # 카드와 함께 붙이는 한 마디. 「이거 좀 이상하지 않아?」


@app.post("/api/mention")
def mention_card(b: MentionCard):
    """손패 카드를 대화에 「언급」한다 — 공개가 아니다.

    실제 테이블에서 «내가 본 게 하나 있는데» 하고 운을 떼는 동작이다. 남들은 그 카드가
    무엇인지(제목·나온 자리)만 알고 내용은 못 본다. 공개는 여전히 별도 행동이라,
    실수로 눌러 목표 카드를 날리는 일이 없다.
    """
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId:
            return JSONResponse({"error": "그 배역으로 말할 수 없습니다"}, status_code=403)
        # 내 손패든 이미 테이블에 깔린 것이든 가리킬 수 있다. 추리는 남이 깐 카드를 두고
        # 「이거 좀 이상하지 않아?」 하는 데서 시작하는데, 여태 자기 카드만 가리킬 수 있었다.
        mine = b.cardId in ROOM["hands"].get(b.roleId, [])
        pub = b.cardId in ROOM["revealed"]
        if not (mine or pub):
            return JSONResponse({"error": "손패에도 테이블에도 없는 카드입니다"}, status_code=409)
        c = SC.get_card(b.cardId)
        if not c:
            return JSONResponse({"error": "없는 카드"}, status_code=404)
        who = SC.get_character(b.roleId) or {}
        nm = who.get("name", b.roleId)
        where = f'{c["locName"]} · {c["spot"]}' if c.get("spot") else c["locName"]
        say = (b.text or "").strip()[:400]
        head = (f'{nm}{_subj(nm)} [{where}] 「{c["title"]}」{_obj(c["title"])} 손에 쥐고 있다고 말했습니다.'
                if mine else
                f'{nm}{_subj(nm)} [{where}] 「{c["title"]}」{_obj(c["title"])} 가리켰습니다.')
        ROOM["table"].append({
            "kind": "cardref", "roleId": b.roleId, "speaker": nm,
            "cardId": c["id"], "cardTitle": c["title"], "cardWhere": where,
            "mine": mine, "say": say, "text": head,
        })
        if say:                       # 한 마디를 붙였으면 그건 그 배역이 실제로 한 말이다
            ROOM["table"].append({"kind": "human", "roleId": b.roleId, "speaker": nm, "text": say})
        _ev("mention", roleId=b.roleId, speaker=nm, cardId=c["id"], title=c["title"])
        bump()
    return {"ok": True}


@app.post("/api/human-say")
def human_say(b: HumanSay):
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId:
            return JSONResponse({"error": "그 배역으로 말할 수 없습니다"}, status_code=403)
        c = SC.get_character(b.roleId)
        ROOM["table"].append({"kind": "human", "roleId": b.roleId, "speaker": c["name"], "text": b.text.strip()})
        _ev("say", who="human", roleId=b.roleId, speaker=c["name"], text=b.text.strip())
        bump()
    return {"ok": True}


class Busy(RuntimeError):
    """다른 배역이 말하는 중."""


def _role_prompt(role_id: str, nudge: str = "") -> str:
    """그 배역 하나짜리 연기 지시문.

    들어가는 것: 공개된 카드(모두가 아는 것) + **그 배역 자신의 손패**.
    들어가지 않는 것: 남의 손패, 남의 비밀, 진상.
    조사해놓고 자기가 뭘 찾았는지 모르면 그걸로 방어도 추궁도 못 한다.
    """
    with LOCK:
        seq = ROOM["seq"]
        revealed = list(ROOM["revealed"])
        table = list(ROOM["table"])
        hand = list(ROOM["hands"].get(role_id, []))   # 자기 것만. 남의 손패는 넘기지 않는다.
        cs = (ROOM.get("crisis") or {}).get("solved")
    c = SC.get_character(role_id)
    for args in ((nudge, hand, cs), (nudge, hand), (nudge,), ()):   # 아직 이걸 안 받는 시나리오도 있다
        try:
            return SC.build_play_prompt(c, seq, revealed, table, *args)
        except TypeError:
            continue
    return SC.build_play_prompt(c, seq, revealed, table)


def _speak(role_id: str, nudge: str = "", fallback: str = "") -> dict:
    """AI 배역 한 명에게 한마디 시킨다. 프롬프트는 그 배역 것만 들어간다.

    fallback은 모델이 빈손으로 돌아왔을 때 대신 나가는 한 줄이다. 「…」만 남는 것보다
    정해둔 문장이라도 나가는 편이 낫다 — 특히 카드를 방금 내려놓은 직후가 그렇다.
    """
    with LOCK:
        r = ROOM["roles"].get(role_id)
        if not r or r["mode"] != "ai":
            raise ValueError("AI 배역이 아닙니다")
        if ROOM["typing"]:
            raise Busy("다른 배역이 말하는 중입니다")
        ROOM["typing"] = role_id
        bump()
        seq = ROOM["seq"]
        revealed = list(ROOM["revealed"])
        table = list(ROOM["table"])
    c = SC.get_character(role_id)
    try:
        system = _role_prompt(role_id, nudge)
        reply = llm(system, f"이제 '{c['name']}'로서 다음 한마디를 하라 (1~3문장).", 400, fast=True)
        reply = re.sub(rf"^{re.escape(c['name'])}\s*[:：]\s*", "", reply or "").strip()
        if not reply:
            reply = fallback
    except Exception:
        if fallback:                       # 모델 쪽이 통째로 막혀도 액션은 나가야 한다
            with LOCK:
                entry = {"kind": "ai", "roleId": role_id, "speaker": c["name"], "text": fallback}
                ROOM["table"].append(entry)
                _ev("say", who="ai", roleId=role_id, speaker=c["name"], text=fallback)
                ROOM["typing"] = None
                bump()
            return entry
        with LOCK:
            ROOM["typing"] = None
            bump()
        raise
    entry = {"kind": "ai", "roleId": role_id, "speaker": c["name"], "text": reply or "…"}
    with LOCK:
        ROOM["table"].append(entry)
        _ev("say", who="ai", roleId=role_id, speaker=c["name"], text=entry["text"])
        ROOM["typing"] = None
        bump()
    return entry


@app.post("/api/ai-say")
def ai_say(b: RoleOnly):
    try:
        _speak(b.roleId)
    except Busy as e:
        return JSONResponse({"error": str(e)}, status_code=429)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"ok": True}


def _addressed_in(text: str, exclude: str = "") -> list[str]:
    """대사 안에서 이름이 불린 배역들. 이름을 부르면 그 사람이 대답한다 — 대화가 이어지는 핵심."""
    out = []
    for ch in SC.CHARACTERS:
        if ch["id"] == exclude:
            continue
        if ch["name"] in text or ch["name"][1:] in text:   # '문재이' / '재이' 둘 다
            out.append(ch["id"])
    return out


def _pick_reactor(exclude: list[str] | None = None) -> str | None:
    """지금 한마디 하기에 가장 자연스러운 AI 배역을 고른다.

    진행 세션이 매번 '누가 말할 차례냐'를 직접 판단하면 부담이 크고, 결국 한두 명만
    계속 떠들게 된다. 그래서 서버가 고른다 — 최근에 말한 사람은 빼고, 방금 공개된
    카드가 자기 구역·관심사인 배역에 가중치를 준다.
    """
    exclude = set(exclude or [])
    with LOCK:
        ais = [rid for rid, r in ROOM["roles"].items() if r["mode"] == "ai" and rid not in exclude]
        if not ais:
            return None
        # 최근 발언자 2명은 연속 발언을 피한다(그래도 후보가 없으면 되살린다)
        recent = [t.get("roleId") for t in ROOM["table"][-4:] if t.get("roleId")]
        fresh = [rid for rid in ais if rid not in recent[-2:]] or ais
        last_card = ROOM["revealed"][-1] if ROOM["revealed"] else None
        seq = ROOM["seq"]
        last = ROOM["table"][-1] if ROOM["table"] else {}
        last_text = str(last.get("text") or "")
        last_from = str(last.get("roleId") or "")
        spoke_n = {rid: sum(1 for t in ROOM["table"] if t.get("roleId") == rid) for rid in ais}
    card = SC.get_card(last_card) if last_card else None
    ai_cfg = getattr(SC, "INVEST_AI", {})
    chat_cfg = getattr(SC, "CHAT_AI", {})
    called = set(_addressed_in(last_text, exclude=last_from))

    def score(rid: str) -> tuple[float, int]:
        s = 0.0
        cfg = ai_cfg.get(rid) or {}
        if rid in called:
            s += 8.0                                       # 이름이 불렸으면 대답할 차례다
        if card:
            if card.get("loc") in (cfg.get("home") or []):
                s += 3.0                                   # 방금 열린 곳이 자기 구역이면 할 말이 있다
            s += float((cfg.get("interest") or {}).get(card["id"], 0) or 0)
        s += 2.0 * float((chat_cfg.get(rid) or {}).get("talk", 1.0))   # 원래 말이 많은 사람
        s -= spoke_n.get(rid, 0) * 0.8                     # 적게 말한 사람에게 자리를 준다
        return (s, -zlib.crc32(f"{rid}:{seq}".encode()) % 997)   # 동점은 결정적으로 가른다

    return max(fresh, key=score)


def _pick_nudge(rid: str) -> str:
    """이번 한마디의 결을 고른다. 매번 같은 결이면 금세 기계처럼 들린다."""
    nudges = getattr(SC, "CHAT_NUDGES", {})
    if not nudges:
        return ""
    with LOCK:
        last = ROOM["table"][-1] if ROOM["table"] else {}
        n_lines = len(ROOM["table"])
    ask_w = float((getattr(SC, "CHAT_AI", {}).get(rid) or {}).get("ask", 1.0))
    # 앞사람이 방금 말했으면 받아치는 쪽이 자연스럽고, 정적이 흘렀으면 새 화제를 꺼내야 한다.
    weights = {
        "react": 3.0 if last.get("text") else 0.5,
        "ask":   2.2 * ask_w,
        "press": 1.6 if last.get("kind") in ("ai", "human") else 0.3,
        "raise": 1.4,
        "mood":  0.7 if n_lines % 5 == 0 else 0.25,        # 가끔만. 자주 하면 겉돈다
    }
    keys = [k for k in weights if k in nudges]
    tot = sum(weights[k] for k in keys) or 1.0
    r = random.random() * tot
    for k in keys:
        r -= weights[k]
        if r <= 0:
            return nudges[k]
    return nudges.get("react", "")


# ── 자발 대화 ─────────────────────────────────────────────────────
# AI 배역이 불릴 때만 말하면 자판기처럼 보인다. 조용한 시간이 일정 이상 흐르면
# 서버가 스스로 한 명을 골라 말하게 한다. 사람이 말하면 물러나고, AI만 계속
# 떠들면 간격을 벌려서 사람이 낄 자리를 만든다.
CHAT = {
    "on": os.getenv("AUTO_CHAT", "1") != "0",
    "gap": float(os.getenv("AUTO_CHAT_GAP", "13")),   # 기본 침묵 허용치(초)
    "seenLen": 0, "lastAt": 0.0, "streak": 0, "busy": False,
}


def _chat_gap_now(ph: dict) -> float:
    """지금 얼마나 조용해야 AI가 입을 여는가."""
    gap = CHAT["gap"]
    if int(ph.get("ap", 0) or 0) > 0:
        gap *= 2.6                       # 조사 페이즈 — 다들 카드 보는 중이라 조용해야 한다
    gap *= 1.0 + CHAT["streak"] * 0.55   # AI만 연달아 떠들수록 물러난다
    return gap


def _chatter_tick():
    if not CHAT["on"]:
        return
    now = time.monotonic()
    with LOCK:
        ph = SC.phase_by_seq(ROOM["seq"])
        n = len(ROOM["table"])
        typing = ROOM["typing"]
        last = ROOM["table"][-1] if ROOM["table"] else {}
        has_ai = any(r["mode"] == "ai" for r in ROOM["roles"].values())
    if ph.get("key") == "reveal" or not has_ai:
        return
    if n != CHAT["seenLen"]:              # 방금 누가 말했다 — 시계를 다시 잡는다
        CHAT["seenLen"] = n
        CHAT["lastAt"] = now
        CHAT["streak"] = CHAT["streak"] + 1 if last.get("kind") == "ai" else 0
        return
    if typing or CHAT["busy"]:
        return
    if now - CHAT["lastAt"] < _chat_gap_now(ph):
        return
    CHAT["busy"] = True
    try:
        _run_queued_act() or _run_free_talk()
    except Exception:                    # 한 번 실패해도 루프는 계속 돈다
        pass
    finally:
        CHAT["busy"] = False
        CHAT["lastAt"] = time.monotonic()


def _run_queued_act() -> bool:
    """예약된 액션 하나를 내보낸다. 카드를 내려놓은 직후의 해석, 몰린 사람에 대한 추궁.

    액션은 정적 대기(_chat_gap_now)를 건너뛴다 — 사건과 붙어 있어야 무슨 얘긴지 통한다.
    """
    with LOCK:
        q = ROOM.get("aiActs") or []
        act = None
        while q:
            a = q.pop(0)
            r = ROOM["roles"].get(a["roleId"])
            if r and r["mode"] == "ai":       # 그 사이 사람이 그 배역을 잡았으면 버린다
                act = a
                break
        if not act:
            return False
        seed = f'{act["roleId"]}|{act["kind"]}|{len(ROOM["table"])}'
    _speak(act["roleId"], _act_nudge(act["kind"], act["fmt"]),
           _act_fallback(act["kind"], act["fmt"], seed, act["roleId"]))
    return True


def _run_free_talk() -> bool:
    """정적이 흘렀을 때의 자발 발언. 공개된 것이 한 사람을 가리키면 추궁으로 바꾼다."""
    rid = _pick_reactor()
    if not rid:
        return False
    with LOCK:
        target, why, why1 = _public_suspect(exclude=rid)
        n = len(ROOM["table"])
    # 근거가 쌓였을 때만, 그리고 매번은 아니게. 계속 몰아세우면 대화가 한 방향으로 굳는다.
    if target and why and n % 3 == 0:
        who = (SC.get_character(target) or {}).get("name", "")
        if who:
            fmt = {"who": who, "why": why, "why1": why1}
            _speak(rid, _act_nudge("blame", fmt), _act_fallback("blame", fmt, f"{rid}|{n}", rid))
            return True
    _speak(rid, _pick_nudge(rid))
    return True


def _chatter_loop():
    while True:
        time.sleep(2.0)
        try:
            _chatter_tick()
        except Exception:                # noqa: BLE001
            pass


# ── AI 배역 조사 차례 자동 진행 ────────────────────────────────────────────────
# 사람이 없는 배역의 차례가 오면 아무도 카드를 뽑지 않아 순번이 그 자리에 선다.
# 진행석에서 버튼을 눌러야 넘어가면 호스트가 화면을 닫는 순간 판이 멈추므로,
# 서버가 스스로 처리한다. 다만 차례가 넘어오자마자 해치우면 사람들이 무슨 일이
# 지나갔는지 못 읽는다 — 차례가 AI에게 온 뒤 잠깐 뜸을 들이고 나서 뽑는다.
# 사람 차례는 절대 대신 넘기지 않는다. 서버는 그 자리에서 기다린다.
AUTO_TURN = {
    "on": os.getenv("AUTO_TURN", "1") != "0",
    # AI 차례 하나에 10초를 쓰면 다섯이면 한 바퀴가 1분이다. 사람은 그동안 할 게 없다.
    # 이 시간은 연출이 아니라 순수한 대기라서, 「방금 뭔가 일어났다」가 읽힐 만큼만 남긴다.
    "delay": float(os.getenv("AUTO_TURN_DELAY", "2.5")),  # AI에게 차례가 온 뒤 기다리는 초
    "key": None,        # 지금 재고 있는 차례 (seq, roleId)
    "seq": None,        # idle 카운터를 리셋할 기준 페이즈
    "since": 0.0,       # 그 차례가 시작된 시각(monotonic)
    "idle": 0,          # 아무것도 못 뽑고 넘긴 횟수 — 한 바퀴 헛돌면 멈춘다
}


def _auto_turn_tick():
    if not AUTO_TURN["on"]:
        return
    now = time.monotonic()
    with LOCK:                       # 판단에 필요한 것만 짧게 읽고 바로 놓는다
        seq = ROOM["seq"]
        ph = SC.phase_by_seq(seq)
        turn = ROOM.get("turn")
        started = bool(ROOM.get("started"))
        mode = (ROOM["roles"].get(turn) or {}).get("mode") if turn else None
        remaining = (_ap_for(seq) - _round_checks(turn, current_round(seq))) if turn else 0
        lap = len(_turn_order())
        blocked = _crisis_blocking()   # 침수 대응 중에는 시계도 멈춘다
    if AUTO_TURN["seq"] != seq:      # 페이즈가 바뀌면 헛돌기 카운터를 푼다
        AUTO_TURN["seq"] = seq
        AUTO_TURN["idle"] = 0
    key = (seq, turn)
    if key != AUTO_TURN["key"]:      # 차례가 막 바뀌었다 — 시계를 다시 잡고 이번엔 넘어간다
        AUTO_TURN["key"] = key
        AUTO_TURN["since"] = now
        return
    if not started or ph.get("key") != "invest" or not turn or blocked:
        return
    if mode != "ai":                 # 사람 차례 — 대신 넘기지 않는다
        return
    if remaining <= 0:               # 이번 라운드 몫을 이미 다 썼다(페이즈 진행은 호스트 몫)
        return
    if AUTO_TURN["idle"] > lap:      # 한 바퀴 돌도록 아무도 뽑을 게 없었다 — 그만 돌린다
        return
    if now - AUTO_TURN["since"] < AUTO_TURN["delay"]:
        return
    with LOCK:
        # 기다리는 사이 판이 바뀌었을 수 있다 — 잡고 나서 한 번 더 확인한다
        if (ROOM["seq"], ROOM.get("turn")) != key or not ROOM.get("started"):
            return
        if SC.phase_by_seq(ROOM["seq"]).get("key") != "invest":
            return
        if (ROOM["roles"].get(turn) or {}).get("mode") != "ai":
            return
        picks = _ai_take_turn(turn)
    AUTO_TURN["idle"] = 0 if picks else AUTO_TURN["idle"] + 1
    AUTO_TURN["since"] = time.monotonic()


def _auto_turn_loop():
    while True:
        # 틱이 2초면 2.5초짜리 대기가 실제로는 4초가 된다. 다섯 명이면 그 차이가 10초다.
        time.sleep(0.5)
        try:
            _auto_turn_tick()
        except Exception:                # noqa: BLE001  한 번 실패해도 루프는 계속 돈다
            pass


@app.on_event("startup")
def _start_loops():
    threading.Thread(target=_chatter_loop, daemon=True).start()
    threading.Thread(target=_auto_turn_loop, daemon=True).start()


@app.post("/api/chat")
def chat_ctl(b: ChatCtl):
    """자발 대화 on/off와 속도. 토론이 뜨거우면 끄고, 식으면 켠다."""
    if not (_agent_ok(b.key) or _is_host(b.clientId) or ROOM.get("host") is None):
        return JSONResponse({"error": "권한 없음"}, status_code=403)
    if b.on is not None:
        CHAT["on"] = bool(b.on)
        CHAT["lastAt"] = time.monotonic()
    if b.gap is not None:
        CHAT["gap"] = max(4.0, min(90.0, float(b.gap)))
    with LOCK:
        bump()
    return {"ok": True, "on": CHAT["on"], "gap": CHAT["gap"]}


@app.post("/api/ai-react")
def ai_react(b: TurnReq):
    """AI 배역 중 하나를 골라 한마디 시킨다 — 진행 세션의 '누구 시킬까' 부담을 덜어준다."""
    rid = b.roleId or _pick_reactor()
    if not rid:
        return JSONResponse({"error": "AI 배역이 없습니다"}, status_code=409)
    try:
        entry = _speak(rid, _pick_nudge(rid))
    except Busy as e:
        return JSONResponse({"error": str(e)}, status_code=429)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=502)
    CHAT["lastAt"] = time.monotonic()          # 방금 말했으니 자발 발언 시계를 다시 잡는다
    return {"ok": True, "roleId": rid, "speaker": entry["speaker"], "text": entry["text"]}


def _grade(c: dict, answers: list[str]) -> dict:
    raw = llm(SC.build_grade_prompt(c, answers), "채점 JSON만 출력하라.", 500)
    o = _parse_json(raw)
    ncount = len(c["sins"]) if c["sins"] else 0
    g = {
        "name": c["name"],
        "selfAccused": bool(o.get("selfAccused", False)),
        "sinsAcknowledged": max(0, min(ncount, int(o.get("sinsAcknowledged", 0) or 0))),
        "osewonIdentified": bool(o.get("osewonIdentified", False)),
        "score": max(0, min(40, int(o.get("score", 0) or 0))),
        "verdict": str(o.get("verdict", "") or ""),
    }
    # 후더닛 시나리오(예: subway)용 추가 필드 — 있을 때만 보존(자기지목형 시나리오엔 영향 없음).
    if "culpritGuess" in o:
        g["culpritGuess"] = str(o.get("culpritGuess") or "unknown")
    if "correct" in o:
        g["correct"] = bool(o.get("correct", False))
    if "cluesFound" in o:
        g["cluesFound"] = max(0, int(o.get("cluesFound", 0) or 0))
    if isinstance(o.get("tags"), list):
        g["tags"] = o["tags"]
    return g


@app.post("/api/final-answers")
def final_answers(b: FinalAnswers):
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId:
            return JSONResponse({"error": "그 배역의 답이 아닙니다"}, status_code=403)
    # 백엔드(API 키)가 없으면 AI 채점 대신 답변을 보관 → 진행자(GM)가 채점/엔딩 내레이션
    if not backend_ready()[0]:
        with LOCK:
            ROOM["finalAnswers"][b.roleId] = list(b.answers)
            bump()
        return {"pending": True, "answers": list(b.answers)}
    c = SC.get_character(b.roleId)
    try:
        grade = _grade(c, b.answers)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=502)
    with LOCK:
        ROOM["grades"][b.roleId] = grade
        ROOM["finalAnswers"][b.roleId] = list(b.answers)
        bump()
    return {"grade": grade}


@app.post("/api/ai-final")
def ai_final(b: RoleOnly):
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["mode"] != "ai":
            return JSONResponse({"error": "AI 배역이 아닙니다"}, status_code=409)
        revealed = list(ROOM["revealed"])
        table = list(ROOM["table"])
    c = SC.get_character(b.roleId)
    try:
        raw = llm(SC.build_final_answer_prompt(c, revealed, table), "JSON만 출력하라.", 700)
        answers = _parse_json(raw).get("answers", [])
        if not isinstance(answers, list) or not answers:
            answers = ["(답변 없음)"]
        grade = _grade(c, [str(x) for x in answers])
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=502)
    with LOCK:
        ROOM["grades"][b.roleId] = grade
        bump()
    return {"answers": answers, "grade": grade}


@app.post("/api/reset")
def reset(b: HostReq):
    global ROOM
    with LOCK:
        if ROOM.get("host") not in (None, b.clientId) and not _agent_ok(b.key):
            return JSONResponse({"error": "host"}, status_code=403)
        ROOM = fresh_room()
        # 호스트도 함께 푼다. 붙들고 있으면 그 브라우저가 사라졌을 때 방이 영영 잠긴다 —
        # 초기화한 사람은 곧바로 다시 잡으면 된다(클라이언트가 이어서 요청한다).
    return {"ok": True}


# 화면은 매번 다시 물어보게 한다. ETag 만 보내고 Cache-Control 을 안 주면 브라우저가
# 제멋대로 「아직 신선하다」고 판단해 옛 파일을 그대로 쓴다 — 고친 게 안 나온다는 신고의 대부분이 이것이었다.
# no-cache 는 「쓰지 마라」가 아니라 「쓰기 전에 물어봐라」다. 안 바뀌었으면 304로 값싸게 끝난다.
_NOCACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/")
def landing():
    # 노아르 허브(로고·포스터·호스트/참가자) — 여기서 사건을 골라 /play 로 진입
    p = _HERE / "landing.html"
    return FileResponse(p if p.exists() else _HERE / "index.html", headers=_NOCACHE)


@app.get("/play")
def play():
    return FileResponse(_HERE / "index.html", headers=_NOCACHE)


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    import uvicorn
    ok, label = backend_ready()
    ip = lan_ip()
    print("=" * 56)
    print("  PIMMmurderboard · 졸업사진(卒業寫眞)")
    print(f"  AI 백엔드: {label}" + ("" if ok else "  ⚠ (미준비 — .env 확인)"))
    print("  브라우저에서 열기:")
    print(f"    이 컴퓨터    →  http://127.0.0.1:{PORT}")
    print(f"    같은 와이파이 →  http://{ip}:{PORT}   (폰·다른 PC는 이 주소로)")
    print("=" * 56)
    uvicorn.run(app, host=HOST, port=PORT)
