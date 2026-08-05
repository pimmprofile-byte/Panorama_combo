"""
panorama_combo — 사람 셋이서 하는 머더미스터리 게임 서버.

각자 자기 폰/PC로 접속한다. 서버가 방 상태를 하나 쥐고 각 기기가 폴링으로 따라온다.
배역의 비밀은 그 배역을 맡은 기기에만 내려간다.

**좌석은 전부 사람이다.** AI 배역도, LLM 호출도 없다 — API 키 없이 돈다.
그래서 심층심문(AI에게서 답을 끌어내는 장치)도 없고, 종막 채점도 없다.
엔딩은 종막 지목표만으로 갈린다: 진범을 짚었는가.

실행: pip install -r requirements.txt → python server.py
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

HOST = os.getenv("REUNION_HOST", "0.0.0.0")
# 호스팅(Render 등)은 PORT를 주입 → 그걸 우선 사용, 로컬은 REUNION_PORT/기본값
PORT = int(os.getenv("PORT") or os.getenv("REUNION_PORT", "8790"))


AGENT_KEY = os.getenv("AGENT_KEY", "")  # 진행석(코드 세션) 원격 조종 키. 미설정이면 개방

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
        "cuts": [],               # 조사 중에 튼 짧은 컷(비주얼노벨). 클라이언트가 안 본 것부터 재생한다
        "hands": {},              # roleId -> [cardId] (손패, 비공개 · 조사/마킹 통합)
        "checkedRound": {},       # roleId -> {cardId: round} (턴별 조사 수 제한 계산용)
        "finalAnswers": {},       # roleId -> [답변]. 채점은 없다 — 다 같이 읽는 기록이다
        "gmSeats": {},            # clientId -> 마지막으로 진행석을 켜둔 시각. 방에 진행자가 있는지 판단용
        "podVotes": {},           # roleId -> 태울 사람. 최종 토론에서 사람이 던진 표만 담는다
        "accuse": {},             # roleId -> 지목한 사람. 종막의 범인 지목, 사람 표만
        # 중간 지목 — 판이 끝나기 전에 한 번 이름을 부르는 사건이 있다. 그 표는 사라지지
        # 않고 종막까지 따라간다. seq 를 같이 적어두는 건 그 막에서만 고칠 수 있게 하려고다.
        "vaultRead": [], "accuse1": {"seq": None, "picks": {}},
        # 아이템 수수께끼를 배역별로 몇 번 틀렸는가. 세 번 틀리면 그 사람에게만 힌트가 열린다.
        "puzzleTries": {},        # roleId -> {cardId: 틀린 횟수}
        # 1차 지목에서 압수된 소지품의 임자들. 한 번 압수되면 판이 끝날 때까지 펴져 있다.
        "seized": [],
        # 밤 — 각자 몰래 한 가지를 고르고, 그 조합이 그날 밤에 실제로 일어난 일을 정한다.
        "night": {"open": False, "picks": {}, "result": None},
        # 질문지 — 순서대로 하나씩 묻는다. 안 물어진 것이 남는 게 이 막의 요점이다.
        "ask": {"open": False, "asked": [], "turn": None},
        # 개발자 — 1차 범인지목의 최다 득표자가 그 자리를 받는다(§7-f). 아무도 못
        # 밀어냈으면 빈 몸이 없어 «안 들어온다»(§7-h). 이 칸은 그 사람 본인 말고는
        # 아무에게도 안 나간다 — 「누구인가」는 물론 「들어왔는가」조차.
        "dev": {"decided": False, "id": "", "why": ""},
        # 방탈출 — 열쇠 반쪽이 다 모이면 저절로 열린다. stage 가 이 막의 전부다.
        #   ""(닫힘) → "locked"(보이지만 잠김) → "steps"(퍼즐) → "done"/"closed"
        "escape": {"open": False, "stage": "", "equipped": [], "step": 0,
                   "log": [], "done": None, "fails": 0, "placed": {}},
        # 선지형 질문지 채점 — 답은 각자 것만, 결과는 채점한 뒤에 다 같이 본다.
        "quiz": {"open": False, "answers": {}, "scored": False, "result": None},
        # 자동으로 셀 수 있는 가점만 여기 쌓인다. 사람이 읽어주는 몫은 안 들어온다.
        "scores": {},             # roleId -> {항목: 점수}
        "dest": {},               # roleId -> 향한 곳. 종막에서 각자 정하고, 다 정해야 열린다
        # 결재 결정문 — 자리마다 한 문장씩 고른다. 범인으로 지목된 사람은 결재권을 잃고
        # 그 칸은 남은 사람들의 투표로 찬다. picks 는 최종값, votes 는 잃은 칸의 표다.
        "decision": {"picks": {}, "votes": {}, "extra": ""},
        # 나이 — 배역이 스스로 적는 자리. 시나리오의 AGE_INPUT 에 오른 배역만 적을 수 있고,
        # 적힌 값은 인물정보에서 모두가 본다. 「누가 적을 수 있는가」는 이 방 상태에 안 담긴다.
        "ages": {},               # roleId -> 그 사람이 적어 넣은 나이
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
        fn({"night": dict(ROOM.get("night") or {}), "seq": ROOM.get("seq", 1),
            # 방탈출 판정과 인벤토리도 같은 방식으로 넘겨준다 — 사건이 진엔딩을
            # 가를 때 「문이 열렸는가」를 물어보기 때문이다. 안 받는 사건에서는
            # 이 두 칸이 그냥 무시된다.
            "escape": dict(ROOM.get("escape") or {}),
            "items": {rid: _inventory(rid) for rid in ROOM.get("roles", {})}})
    except Exception:                                   # noqa: BLE001
        pass


def bump():
    ROOM["rev"] += 1
    # 열쇠 반쪽이 다 모였는가 — 「모이면 저절로 열린다」라서 누가 무엇을 해서
    # 모였는지는 안 따진다. 상태가 움직일 때마다 한 번씩 확인한다(열려 있으면 즉시 반환).
    try:
        _escape_try_open()
    except Exception:                                   # noqa: BLE001 — 문 하나 때문에 판이 멈추면 안 된다
        pass
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
    assigned = [rid for rid, r in ROOM["roles"].items() if r["mode"] == "human"]
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
    assigned = [rid for rid, r in ROOM["roles"].items() if r["mode"] == "human"]
    if assigned and len(cr["answers"]) < len(assigned):
        return
    _crisis_resolve()


def _crisis_resolve():
    conf = _crisis_conf()
    cr = ROOM["crisis"]
    if not conf or not cr.get("open"):
        return
    key = SC.crisis_answer_key()
    assigned = [rid for rid, r in ROOM["roles"].items() if r["mode"] == "human"] or list(ROOM["roles"])
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

    ★ 시나리오는 이걸 «문자열 목록»으로 적고, 화면은 «{kind, text} 객체»를 읽는다.
      그 사이를 아무도 안 맞춰 줘서, 여태 카드 위에 이름표(「내 눈에만 걸리는 것」)만
      뜨고 «글은 통째로 비어» 있었다. 모양을 맞추는 자리는 여기다 — 시나리오마다
      객체를 적게 하면 원고 쓰는 사람이 엔진 사정을 알아야 하고, 화면에서 두 모양을
      다 받게 하면 그 분기가 화면마다 늘어난다.
    """
    fn = getattr(SC, "private_notes", None)
    if not role_id or not fn:
        return {}

    def _shape(n):
        # 원고가 그냥 한 줄로 적은 것 — 이 판의 기본이다.
        if isinstance(n, str):
            return {"kind": "eye", "text": n}
        if isinstance(n, dict):
            row = {"kind": n.get("kind") or "eye", "text": n.get("text") or ""}
            if n.get("points"):
                row["points"] = n["points"]
            return row
        return {"kind": "eye", "text": str(n)}

    out = {}
    for cid in card_ids:
        try:
            ns = fn(role_id, cid)
        except Exception:              # noqa: BLE001 — 시나리오가 안 갖췄어도 판은 돌아야 한다
            ns = None
        if ns:
            rows = [_shape(n) for n in ns]
            out[cid] = [r for r in rows if r["text"]]
    return out


def _age_inputs() -> set:
    """나이를 「본인이 적는」 배역들. 시나리오가 정한다 — 엔진은 배역 id를 모른다."""
    return set(getattr(SC, "AGE_INPUT", []) or [])


def _age_display(cid: str) -> str:
    """그 배역의 나이 칸에 «그대로 뜰 글자». 판단을 서버가 끝내서 문자열로 내려보낸다.

    화면이 「이 배역은 나이를 적을 수 있는 자리인가」를 스스로 가르려면 결국 그 명단을
    받아야 하는데, 그 명단이 곧 이 판의 답이다(셋 중 하나만 자기 나이를 댈 수 있다).
    그래서 명단은 내보내지 않고, 나온 글자만 내보낸다.

    적어 넣은 값 → 시나리오가 적어 둔 값 → 둘 다 없으면
    「신원미상」(태어난 날이 없는 자들). 다만 스스로 적는 자리는 아직 안 적었을 뿐이므로
    빈 값으로 두고, 화면이 그 칸을 「—」로 그린다.
    """
    v = str((ROOM.get("ages") or {}).get(cid) or "").strip()
    if v:
        return v
    v = str((SC.get_character(cid) or {}).get("age") or "").strip()
    if v:
        return v
    # 나이 기믹이 없는 사건에서는 빈 칸이 그냥 빈 칸이다 — 없던 「신원미상」을 만들어내지 않는다.
    if not _age_inputs() or cid in _age_inputs():
        return ""
    return str(getattr(SC, "AGE_UNKNOWN", "") or "신원미상")


def public_state() -> dict:
    with LOCK:
        seq = ROOM["seq"]
        ph = SC.phase_by_seq(seq)
        # 엔딩은 종막 지목표가 정한다 — 채점자가 없는 판이다.
        # 사건이 아직 준비가 안 됐다고 보면 None을 돌려준다.
        ending = SC.compute_ending(dict(ROOM["accuse"]))
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
            # 보이는 구역의 지문. 달라지면 화면이 /api/scenario 를 다시 받는다.
            "zoneSig": _zone_sig(),
            "roomId": ROOM.get("roomId", ""),
            "podOpen": bool(ROOM.get("podOpen")),
            "podLaunch": _pod_launch_public(),
            "flood": int(ROOM.get("flood", 0)),
            "crisis": _crisis_public(),
            "night": _night_public(),
            "ask": _ask_public(),
            "accuse1": _accuse1_public(),
            # 방탈출 — 그 막이 열리기 전에는 None 이라 화면 자체가 안 뜬다.
            # 정답은 여기에 한 톨도 안 실린다(고를 것과 물음만 나간다).
            "escape": _escape_public(),
            # 질문지 — 놓이기 전에는 None. 정답·남의 답·채점 결과는 때가 돼야 열린다.
            "quiz": _quiz_public(),
            # 화면이 「질문지가 놓였다(읽기만)」를 이 한 칸으로 본다.
            "finalSheet": _final_sheet(),
            "sealed": list(ROOM.get("sealed") or []),
            "phase": {"seq": ph["seq"], "key": ph["key"], "name": ph["name"], "gm": ph["gm"], "ap": ap, "min": ph["min"],
                      "noMap": bool(ph.get("noMap")),
                      "quota": [{"label": b.get("label", ""), "locs": list(b.get("locs") or []),
                                 "n": int(b.get("n", 0) or 0)} for b in _quota_for(seq)]},
            "roles": {rid: {"mode": r["mode"], "claimed": r["clientId"] is not None} for rid, r in ROOM["roles"].items()},
            # 나이 — 배역마다 화면에 뜰 글자 한 칸. 모두가 같은 것을 본다.
            "ages": {rid: _age_display(rid) for rid in ROOM["roles"]},
            "table": table_tail(),
            "revealed": [SC.public_card(cid) for cid in ROOM["revealed"]],
            "revealedIds": list(ROOM["revealed"]),
            "cuts": list(ROOM.get("cuts") or []),
            "checked": checked,
            "usedAP": used,
            "ready": _ready_state(),
            "belongLimit": _belong_limit(),
            # 구역 몫을 쓰는 조사 페이즈에서, 배역별로 어느 구역을 몇 장 열었는지.
            "quotaUsed": {rid: _quota_used(rid, cur) for rid in ROOM["roles"]},
            "handLimit": _hand_limit(),
            "pod": _pod_state(),
            # seq 8은 잠수정 기준의 매직넘버였다. 사건마다 막 수가 달라서 페이즈 키로 본다.
            "arrest": (_arrest_state() if ph.get("key") in ("final", "decision", "reveal") else None),
            "dest": (_dest_state() if ph.get("key") in ("final", "decision", "reveal") else None),
            "decision": (_decision_state() if ph.get("key") in ("decision", "reveal") else None),
            "keepGoals": _keep_goal_results() if ph.get("key") in ("final", "reveal") else [],
            # 아이템은 상한 밖이다 — _over_limit 과 같은 셈을 쓴다(예전엔 여기서만
            # 손패 전체를 세어서, 도구를 하나 주우면 화면에 「넘쳤다」가 떴다).
            "overLimit": {rid: _over_limit(rid) for rid in ROOM["hands"] if _over_limit(rid) > 0},
            # 누가 도구를 몇 개 모았는가 — 이건 공개 정보다. 이 판의 시계다.
            # 무엇이 나왔는지는 푼 사람만 안다(개수만 나간다).
            # 열쇠 반쪽은 도구가 아니라 입장권이라 이 셈에서 뺀다(§6) — 그건 문 앞에서만 센다.
            "items": {rid: n for rid, n in
                      ((r, len([c for c in _inventory(r)
                                if not (SC.get_card(c) or {}).get("keyHalf")]))
                       for r in ROOM["roles"]) if n},
            "itemNeed": len(getattr(SC, "ESCAPE_ITEMS", []) or []),
            "puzzleOpen": _puzzle_open_now()[0],
            "vault": _vault_public(),
            "turn": ROOM.get("turn") if ap > 0 else None,
            "turnOrder": _turn_order() if ap > 0 else [],
            # 이 라운드에 아직 «누구든» 열 수 있는 자리가 남았는가.
            # 카드보다 턴이 많은 판에서 막바지에 화면이 멈춘 것처럼 보이던 자리다.
            "openLeft": (len(_round_open_pool()) if ap > 0 else None),
            "openIds": (_round_open_pool() if (ap > 0 and DEBUG_POOL) else None),
            "started": bool(ROOM.get("started")),
            "typing": ROOM["typing"],
            "ending": ending,
        }


app = FastAPI(title="GAME DAY")

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
    # QA 검수 모드 — 한 기기가 좌석을 여러 개 쥐겠다는 신고다. 열쇠(key)가 맞아야 통한다.
    # 평소 판은 이 두 값을 안 보내므로 예전과 한 톨도 다르지 않게 돈다.
    qa: bool = False
    key: str = ""


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


class AutoSweep(BaseModel):
    """QA 자동조사 — 「이 배역이 이번 라운드에 할 수 있는 것을 알아서 다 한다」."""
    roleId: str = ""            # 비우면 좌석에 앉은 배역 전부
    key: str = ""
    puzzles: bool = True        # 수수께끼도 대신 풀어줄 것인가


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


class AgeReq(BaseModel):
    roleId: str
    clientId: str
    age: str = ""


@app.get("/api/scenario")
def scenario():
    d = SC.public_scenario()
    # 아직 열리지 않은 구역은 대본에서 통째로 걷어낸다 — 회색으로 걸어두면 그 칸이
    # 「저기 뭔가 더 있다」를 공지한다. 자물쇠 카드가 열리는 순간 이 목록이 늘어나고,
    # 화면은 state 의 zoneSig 로 그걸 알아채 대본을 다시 받는다.
    _hid = _hidden_zones()
    if _hid:
        for _k in ("map", "rooms"):
            if isinstance(d.get(_k), list):
                d[_k] = [z for z in d[_k] if z.get("loc") not in _hid]
        if isinstance(d.get("sealedWhy"), dict):
            d["sealedWhy"] = {k: v for k, v in d["sealedWhy"].items() if k not in _hid}
    # 「나이를 스스로 적는 배역」 명단은 모두가 받는 이 대본에 실으면 안 된다 —
    # 셋 중 하나만 자기 나이를 댈 수 있다는 것이 이 판의 단서라, 명단이 곧 답이다.
    # 시나리오가 실어 보내더라도 여기서 걷어낸다. 그 사실은 /api/state 가
    # «그 배역 본인에게만» ageAsk 한 줄로 알린다.
    d.pop("ageInput", None)
    # 선지형 질문지는 **정답을 달고 있다**(correct · bonus · correctIsDev · noDevKey).
    # 원고가 그 표를 통째로 실어 보내더라도 여기서 물음과 선지만 남기고 걷어낸다 —
    # 모두가 받는 대본에 정답이 실리면 종막의 채점이 아무 뜻이 없다.
    if _quiz_on():
        d["finalQuestions"] = _quiz_sheet()
    # 클라이언트는 STATE.scenarioId와 이걸 비교해 시나리오가 바뀐 걸 알아챈다.
    # 여태 어느 시나리오도 이 값을 안 실어 보내서, 호스트가 시나리오를 바꿔도
    # 다른 기기는 옛 대본을 그대로 들고 있었다.
    d["scenarioId"] = SC.ID
    # 조사카드 카탈로그(제목·본문 제외 — 미공개 슬롯 구조만)
    d["cardCatalog"] = [{"id": c["id"], "loc": c["loc"], "locName": c["locName"], "round": c["round"],
                         "spot": c.get("spot", ""),
                         "needs": _card_needs(c),
                         # auto  판이 스스로 여는 자리 · hot  판을 뒤집는 자리
                         # gone  이 라운드부터는 그 자리가 «없다»(어제와 같은 방이 아니다)
                         "auto": bool(c.get("auto")), "hot": bool(c.get("hot")),
                         "gone": c.get("gone", 0),
                         # locked  수수께끼를 풀어야 열리는 자리(조사턴을 안 쓴다)
                         # item    탈출에 쓰는 도구. 손패 상한 밖의 «인벤토리» 로 들어간다
                         "locked": bool(c.get("puzzle")), "item": bool(c.get("item")),
                         "requires": c.get("requires"), "obligatory": c.get("reveal") == "obligatory"}
                        for c in SC.CARDS if c.get("loc") not in _hid]
    # 화면이 「내가 받은 대본이 지금 판과 같은가」를 이 한 줄로 잰다.
    d["zoneSig"] = _zone_sig()
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
    # 호스트를 쥔 기기이거나, 열쇠를 «실제로 들고 온» 요청(진행석·QA 검수)이다.
    # 여기서만은 빈 키를 안 받는다 — AGENT_KEY를 안 건 로컬 판에서 그걸 통과시키면
    # 가드가 아예 없는 것과 같아서 아무 기기나 남의 방을 갈아엎게 된다(_gm_key_ok 주석).
    mine = (held in (None, b.clientId)) or _gm_key_ok(b.key) or _key_host(b.key)
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
def state(clientId: str = "", gm: int = 0, roleId: str = "", key: str = ""):
    """방 상태 한 벌. 비밀(내 메모·내 소지품·내 밤·내 표)은 「나」의 몫만 실린다.

    roleId/key 는 QA 검수 모드 전용이다 — 둘 다 안 보내면 예전과 완전히 같은 길로 간다.
    """
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
        # ── 「나」를 여기서 한 번만 정한다 ──────────────────────────────
        # 아래로 내려가는 비밀(추가 정보·소지품·밤의 선택·내 표·내 메모)은 전부 이 한 사람의 것이다.
        # 예전엔 같은 찾기를 대여섯 번 되풀이했다 — 시점을 갈아끼우려면 그게 다 갈라진다.
        #
        # QA 검수 모드: 혼자 세 좌석을 쥔 검수자는 clientId만으로 배역이 하나로 안 좁혀진다
        # (첫 번째 좌석만 잡힌다). 그래서 시점을 명시로 받되 문을 세 겹으로 잠근다 —
        #   ① AGENT_KEY(진행석·관리자 창구와 같은 열쇠)를 맞출 것
        #   ② 그 좌석을 실제로 «내가» 쥐고 있을 것 (남의 자리는 열쇠가 있어도 못 본다)
        #   ③ roleId 를 안 보내면 이 갈래를 아예 지나가지 않을 것
        # 그래서 열쇠 없는 요청은 예전과 한 톨도 다르지 않게 돈다.
        qa_role = ""
        if roleId and clientId and _agent_ok(key):
            _seat = ROOM["roles"].get(roleId) or {}
            if _seat.get("clientId") == clientId:
                qa_role = roleId
        me = qa_role or next((rid for rid, r in ROOM["roles"].items()
                              if clientId and r["clientId"] == clientId), "")
        if qa_role:
            st["qaRole"] = qa_role       # 서버가 이 시점을 받아들였다는 확인 — 화면이 이걸로 표시한다
        # 나이를 스스로 적는 자리인가 — «그 배역 본인에게만» 알린다.
        # 남의 화면에 이 표시가 실리면 「셋 중 하나만 나이를 댈 수 있다」가 판 밖에서 풀린다.
        # 그래서 명단이 아니라 「나는 적을 수 있다」 한 줄만, 그 사람에게만 내려간다.
        if me and me in _age_inputs():
            st["ageAsk"] = {"mine": str((ROOM.get("ages") or {}).get(me) or "")}
        # 「추가 정보」가 몇 장인가. 화면은 이 숫자가 늘어난 것만 보고 알림을 띄운다 —
        # 무엇이 늘었는지는 열어봐야 안다.
        mine0 = me
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
            # 문이 열렸으면 클리어 정보가 전원에게 한 장 열린다.
            if (ROOM.get("escape") or {}).get("done"):
                n += 1
            # 개발자가 된 사람에게만 한 장 더. 남의 셈에는 안 붙는다.
            if _dev_me(mine0):
                n += 1
            st["extraN"] = n
            # 자동으로 셀 수 있는 가점. 남의 것은 안 나간다.
            sc = (ROOM.get("scores") or {}).get(mine0) or {}
            if sc:
                st["myScore"] = {"items": dict(sc), "total": sum(int(v) for v in sc.values())}
        # 자기가 이미 적었는지는 자기만 안다. 남이 무엇을 적었는지는 다 던진 뒤에 열린다.
        if st.get("accuse1") is not None:
            who = me
            # 내가 무엇을 적었는지는 언제든 나만 볼 수 있다. 남의 표는 종막까지 안 열린다.
            st["accuse1"]["mineDone"] = who in ((ROOM.get("accuse1") or {}).get("picks") or {})
            st["accuse1"]["mine"] = ((ROOM.get("accuse1") or {}).get("picks") or {}).get(who, "")
        # 개발자 — «그 사람 본인에게만». 아닌 사람에게는 이 칸이 아예 없다.
        # 「너는 개발자가 아니다」도, 「누군가 개발자가 됐다」도 안 나간다.
        _dv = _dev_me(me)
        if _dv:
            st["dev"] = _dv
            mc = _dev_my_cuts(me)
            if mc:
                # 그 사람 몫의 컷. 같은 id 의 공통 컷이 있으면 화면이 그것을 갈아 끼운다.
                st["myCuts"] = list(st.get("myCuts") or []) + mc
        # 방탈출 — 남는 열쇠가 내 것인지, 클리어 뒤의 내 몫이 무엇인지가 사람마다 다르다.
        if st.get("escape") is not None and me:
            st["escape"] = _escape_public(me)
        # 질문지 — 내가 무엇을 적었는지는 나만 본다.
        if st.get("quiz") is not None and me:
            st["quiz"] = _quiz_public(me)
        # 소지품 — 내 것과, 압수돼 펴진 것만. 남의 주머니는 압수 전에는 안 보인다.
        _who = me
        _bl = _belongings_public(_who)
        if _bl:
            st["belongings"] = _bl
        # 밤의 선택지는 그 사람 것만 내려간다. 공개 상태에는 «누가 정했나»만 있다.
        if st.get("night") is not None:
            mine = me
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
        st["myRole"] = me or None
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
        # 호스트 자리를 지키는 신호는 «진짜» 호스트만 남긴다. 열쇠로 서 있는 사람이
        # 이걸 갱신하면, 정작 사라진 호스트의 자리가 영영 안 비어서 아무도 못 이어받는다.
        _real_host = bool(clientId) and ROOM.get("host") == clientId
        if _real_host:
            ROOM["hostSeen"] = time.time()
        # QA 검수 모드는 열쇠로 호스트 권한을 빌린다. 클라이언트의 진행 UI가 이 한 값에
        # 걸려 있어서, 여기서 안 세워주면 세 좌석을 다 쥐고도 페이즈를 못 넘긴다.
        # ROOM["host"] 는 안 건드린다 — 빌리는 것이지 빼앗는 것이 아니다(_host_ok 참고).
        st["isHost"] = _real_host or _key_host(key)
        # 호스트를 쥔 기기가 사라지면(창을 닫았거나, 저장소를 지웠거나, 다른 폰으로 옮겼거나)
        # 아무도 판을 못 굴린다. 그 자리는 잠깐 비면 남이 이어받을 수 있어야 한다.
        st["hostStale"] = bool(ROOM.get("host")) and not st["isHost"] and _host_stale()
        # 호스트를 아무도 안 잡은 방도 있다. 그때는 '호스트 전용' 연출을 아무도 못 보게 되므로
        # 클라이언트가 그 사정을 알 수 있게 해준다(다른 엔드포인트도 같은 규칙으로 통과시킨다).
        # 열쇠로 서 있는 사람에게는 «호스트가 있다»로 답한다 — 자기가 그 호스트인데
        # 「호스트를 기다립니다」가 뜨면 앞뒤가 안 맞는다.
        st["hasHost"] = (ROOM.get("host") is not None) or st["isHost"]
    return st


def _seed_alibi() -> None:
    """알리바이 한 바퀴를 대화창에 깔아둔다.

    예전에는 오프닝 화면 옆의 접힌 패널이었다. 거기 두면 조사 페이즈로 넘어가는 순간
    화면에서 사라져서, 정작 대조가 필요한 토론 때 아무도 다시 못 봤다. 대화 기록으로
    남겨두면 위로 올려 언제든 다시 읽을 수 있다.
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
        if ROOM.get("host") is not None and not _host_ok(b.clientId, b.key):
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
        # 처음부터 손에 쥐고 시작하는 물건(START_ITEMS). 데이지의 확성기처럼 «조사해서
        # 얻는 것이 아니라 원래 갖고 있던» 물건이다. 여태 시나리오가 적어만 놓고
        # 나눠주는 자리가 없어서 아무도 못 들고 시작했다.
        for rid, cids in (getattr(SC, "START_ITEMS", {}) or {}).items():
            if rid not in ROOM["roles"]:
                continue
            h = ROOM["hands"].setdefault(rid, [])
            for cid in cids:
                if cid not in h:
                    h.append(cid)
                    ROOM["checkedRound"].setdefault(rid, {})[cid] = 0
        _reveal_autos()          # 오프닝(0라운드)에서 스스로 열리는 자리
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
        # 평소에는 한 기기가 한 자리다 — 다른 배역을 고르면 앞자리를 놓는다.
        # QA 검수 모드에서만 이 놓기를 건너뛴다. 원고를 쓰는 사람은 혼자 들어와
        # 세 배역을 다 겪어봐야 하는데, 좌석이 전부 사람이라 셋이 안 차면 판이 시작되지 않는다.
        # 열쇠는 진행석·관리자 창구와 같은 것(AGENT_KEY)을 쓴다 — 새 인증을 만들지 않는다.
        if not (b.qa and _agent_ok(b.key)):
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


@app.get("/api/sheet/{role_id}")
def sheet(role_id: str, clientId: str = ""):
    with LOCK:
        r = ROOM["roles"].get(role_id)
        seq = ROOM["seq"]
        if not r:
            return JSONResponse({"error": "없는 배역"}, status_code=404)
        # 엄격: 내가 '맡은' 배역만 (빈자리 비밀 열람 차단).
        # QA 검수 모드에도 이 자물쇠를 그대로 둔다 — 검수자는 세 좌석을 «실제로 쥐고» 들어오므로
        # 여기서 열쇠를 따로 볼 필요가 없다. 문을 하나 더 내면 그 문이 언젠가 열린 채로 남는다.
        if r["clientId"] != clientId:
            return JSONResponse({"error": "자기 배역만 열람할 수 있습니다"}, status_code=403)
    s = SC.private_sheet(role_id)
    # 개발자가 된 사람의 롤카드는 «갈아 끼워진다» — 덧붙는 것이 아니다. 개발자가 된
    # 순간 예전 목표는 이미 다 아는 것이 되어 사라진다. 이 창구는 본인만 열 수 있으니
    # 여기서 덮어도 남에게는 한 톨도 안 간다.
    _dv = _dev_me(role_id)
    if s and _dv and _dv.get("sheet"):
        s.update(_dv["sheet"])
        s["dev"] = True
    # 나이는 롤카드에도 «지금 값»으로 뜬다. 스스로 적는 배역이면 적어 넣은 것이,
    # 아니면 「신원미상」이 온다. 이 창구는 본인만 열 수 있으니 여기서는 ageInput 을 실어도 된다 —
    # 자기가 적을 수 있다는 건 자기가 알아야 할 일이다.
    if s:
        s["age"] = _age_display(role_id)
        s["ageInput"] = role_id in _age_inputs()
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


@app.post("/api/age")
def set_age(b: AgeReq):
    """자기 나이를 적어 넣는다 — 시나리오가 지정한 배역이, 그 배역을 맡은 기기에서만.

    게임 밖의 숫자가 게임 안으로 들어오는 자리다. 적힌 값은 인물정보에서 모두가 본다.

    ★ 되돌아오는 말은 두 갈래 다 똑같은 403이다. 「그 자리가 아니다」와 「적을 수 있는
      배역이 아니다」를 갈라 말하면, 남의 배역 id를 넣어보는 것만으로 누가 적을 수 있는
      자리인지가 드러난다 — 그게 이 판의 답이다.
    ★ **한 번 적으면 끝이다.** 예전에는 고칠 수 있게 두었는데(오타 걱정), 고칠 수
      있으면 그 숫자가 판 위의 사실이 안 된다 — 남이 「몇 살이냐」고 물어 몰린 뒤에
      슬쩍 바꿔 대는 길이 열린다. 이 판에서 나이는 심문의 대상이라 흔들리면 안 된다.
      그래서 화면에서도 「고치기」를 없앴고, 여기서도 두 번째 요청은 안 받는다.
      화면만 막으면 저장소를 지우거나 다른 기기로 들어와 다시 적을 수 있다.
    ★ 여기서 돌아가는 409 는 「내가 이미 적었다」는 말뿐이라, 적을 수 있는 자리인지를
      모르는 사람에게는 아무것도 안 알려준다 — 그 앞의 403 을 먼저 맞기 때문이다.
    """
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if b.roleId not in _age_inputs() or not r or r["clientId"] != b.clientId:
            return JSONResponse({"error": "권한 없음"}, status_code=403)
        if (ROOM.get("ages") or {}).get(b.roleId):
            return JSONResponse({"error": "나이는 한 번만 적을 수 있습니다"}, status_code=409)
        v = re.sub(r"\s+", " ", str(b.age or "")).strip()[:12]
        if not v:
            return JSONResponse({"error": "나이를 적어주세요"}, status_code=400)
        ROOM.setdefault("ages", {})[b.roleId] = v
        bump()
    return {"ok": True, "age": v}


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


def _key_host(key: str) -> bool:
    """열쇠를 «실제로 들고 온» 요청인가 — QA 검수 모드가 이 길로 들어온다.

    QA 검수는 한 사람이 세 좌석을 다 쥐고 혼자 판을 굴려보는 모드다. 그 사람이
    호스트까지 쥐고 있으리란 보장이 없어서, 여태 페이즈를 넘길 수가 없었다.
    열쇠(AGENT_KEY)는 진행석·관리자 창구와 같은 것을 쓴다 — 새 인증을 만들지 않는다.

    _agent_ok 를 그냥 쓰지 않는 이유: 그건 AGENT_KEY를 안 건 서버에서 «빈 키»에도
    True를 준다. 그래서 키를 안 보낸 평범한 참가자까지 호스트가 돼버린다.
    여기서는 빈 키를 먼저 잘라내므로, 열쇠 없는 요청의 길은 한 톨도 안 바뀐다.
    """
    return bool(key) and _agent_ok(key)


def _host_ok(client_id: str, key: str) -> bool:
    """이 요청을 호스트 권한으로 볼 것인가 — 진행 계열 엔드포인트가 전부 이 하나를 본다.

    호스트를 쥔 기기이거나, 열쇠를 맞춘 요청(진행석·QA 검수)이다. 어디까지나
    「이 요청은 호스트 권한으로 본다」일 뿐 ROOM["host"] 자체는 건드리지 않는다 —
    QA가 진짜 호스트의 자리를 빼앗으면 그쪽 화면에 «권한이 넘어갔습니다»가 뜬다.
    """
    return _is_host(client_id) or _agent_ok(key)


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


def _gated_zones() -> dict:
    """수수께끼를 풀어야 «생기는» 구역 → 그 자물쇠 카드 id.

    원고는 이걸 카드 쪽에 `unlockZone` 한 줄로 적는다(F1 → 하늘 끝, D4 → 바다 끝).
    구역 쪽에 따로 표를 두지 않는 것은, 자물쇠가 곧 그 구역의 «입구» 라서다.
    """
    out = {}
    for c in getattr(SC, "CARDS", []) or []:
        z = c.get("unlockZone")
        if z:
            out.setdefault(z, c["id"])
    return out


def _hidden_zones() -> set:
    """지금 **화면에 아예 없어야 할** 구역들.

    잠긴 구역을 회색으로 걸어두는 것과 아예 안 보이는 것은 전혀 다른 판이다.
    회색 칸은 「저기 뭔가 더 있다」를 그 자리에서 공지한다 — 하늘 끝과 바다 끝은
    «있는 줄도 몰랐던 곳이 열리는» 자리라서, 열리기 전에는 지도에 없어야 한다.

    그래서 여기서 걸러낸 구역은 대본(`/api/scenario`)에서 통째로 빠진다.
    자물쇠 카드가 공개되는 순간 목록이 바뀌고, 화면은 `zoneSig` 가 달라진 것을
    보고 대본을 다시 받는다 — 그 한 박자가 없으면 새 구역의 카드가 손에 들어왔는데
    카탈로그에는 없는 상태가 잠깐 생긴다.
    """
    seen = set(ROOM.get("revealed") or [])
    for cids in ROOM.get("hands", {}).values():
        seen.update(cids)
    return {z for z, cid in _gated_zones().items() if cid not in seen}


def _zone_sig() -> str:
    """지금 보이는 구역들의 지문. 이 값이 바뀌면 화면이 대본을 다시 받는다."""
    hid = _hidden_zones()
    return ",".join(sorted(z["loc"] for z in (getattr(SC, "MAP", []) or [])
                           if z.get("loc") not in hid))


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
    """손패를 «일반 단서»와 «소지품» 두 칸으로 가른다. 상한이 서로 다르다.

    아이템(item)은 어느 쪽에도 안 든다 — 탈출에 쓰는 «인벤토리» 라서 상한이 없다.
    여기 넣으면 손패 상한 1장짜리 판에서 도구를 하나 줍는 순간 판이 멈춘다.
    """
    bl = set(_belong_locs())
    clue, belong = [], []
    for cid in ROOM["hands"].get(role_id, []):
        c = SC.get_card(cid) or {}
        if _is_gear(c):
            continue
        (belong if c.get("loc") in bl else clue).append(cid)
    return clue, belong


def _is_gear(card: dict) -> bool:
    """상한 밖의 물건인가 — 도구(`item`)와 열쇠 반쪽(`keyHalf`).

    열쇠 반쪽은 도구가 아니라 «입장권»이라 조합에는 안 쓴다. 그래도 손패 상한에
    걸리면 안 된다 — 상한이 1장인 판에서 반쪽 하나를 주우면 그 자리에서 판이 멈춘다.
    """
    return bool((card or {}).get("item") or (card or {}).get("keyHalf"))


def _inventory(role_id: str) -> list:
    """그 사람이 풀어서 얻은 도구·열쇠들. 상한이 없고, 판이 끝날 때까지 손에 남는다."""
    return [cid for cid in ROOM["hands"].get(role_id, [])
            if _is_gear(SC.get_card(cid) or {})]


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


# ── 카드가 열리는 조건 (선행 단서 · 라운드 · 구역 잠금) ────────────────
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





def _crisis_blocking() -> bool:
    """지금 침수 대응 판정이 걸려 있는가. 걸려 있으면 조사도 차례 넘김도 멈춘다."""
    cr = ROOM.get("crisis") or {}
    return bool(cr.get("open")) and cr.get("solved") is None


def _try_investigate(role_id: str, card_id: str, enforce_ap: bool = True, enforce_turn: bool = False,
                     _puzzle_bypass: bool = False) -> str | None:
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
    # 아이템은 조사턴으로 못 엽니다. 수수께끼를 푸는 것이 곧 여는 방법입니다 —
    # 그래서 이 여섯 장은 조사 예산 밖에 있습니다.
    if (c.get("puzzle") and card_id not in ROOM["revealed"] and not _puzzle_bypass
            and not _holder_of(card_id)):
        # 이미 누가 풀어 간 카드라면 아래 «임자» 안내가 더 맞는 말이다 — 그쪽에 양보한다.
        return "여기는 뒤져서 여는 자리가 아닙니다 — 수수께끼를 풀어야 열립니다"
    if c.get("gone") and cur >= c["gone"] and card_id not in ROOM["hands"].get(role_id, []):
        return "그 자리는 이제 없습니다 — 어제와 같은 방이 아닙니다"
    if c["round"] > cur:
        return f"아직 조사할 수 없습니다 (조사 R{c['round']}에 열림)"
    # 선행 카드 검사. 여태 _openable_cards(=화면 표시)에만 있었고 실제 조사 요청에는
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
    다른 카드를 내려놓아야 한다 — 넘치면 화면이 경고한다.
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


def _reveal_autos() -> None:
    """`auto` 카드를 때가 되면 판이 스스로 연다.

    여태 이걸 여는 자리가 아무 데도 없었다 — `auto: True` 는 「뒤져서 못 연다」만 하고
    「그럼 언제 열리나」를 아무도 안 했다. 그래서 A1(조각난 몸)처럼 사건의 첫 장이
    판이 끝날 때까지 안 나왔고, 그걸 선행조건으로 건 A9 는 영영 잠겨 있었다.

    규칙은 하나다 — 그 라운드가 왔고 구역이 열려 있으면 전체공개한다.
    수수께끼가 걸린 아이템은 예외다. 그건 「때가 되면」이 아니라 「풀면」 열린다.
    """
    cur = current_round(ROOM["seq"])
    for c in SC.CARDS:
        if not c.get("auto") or c.get("puzzle"):
            continue
        if c["id"] in ROOM["revealed"] or c["round"] > cur:
            continue
        if _zone_lock(c.get("loc", ""), cur):
            continue
        if c.get("gone") and cur >= c["gone"]:
            continue
        _publish(c["id"])


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


class PuzzleTry(BaseModel):
    cardId: str
    roleId: str
    clientId: str
    answer: str = ""


PUZZLE_HINT_AFTER = 2        # 이만큼 틀리면 그 사람에게만 힌트 한 줄이 열린다
# ── 세 번이면 끝 ──────────────────────────────────────────────────
# 틀려도 아무 일이 안 일어나면 답을 «찍는» 것이 제일 싼 수가 된다. 세 번으로 끊으면
# 한 번 넣기 전에 카드를 다시 읽게 된다 — 그게 이 판이 바라는 행동이다.
#
# ★ **잠기는 것은 그 사람에게만이다.** 판 전체에서 잠그면 F1(피아노 아래)·D4(계기반)
#   처럼 구역을 여는 수수께끼가 영영 안 열려서 하늘 끝·바다 끝에 아무도 못 들어가고,
#   열쇠 반쪽이 안 모여 방탈출 막이 통째로 사라진다. 사람마다 세 번이니 한 수수께끼에
#   판 전체로는 아홉 번이 있고, 못 푼 사람은 옆사람에게 넘겨야 한다 — 3인 판에서
#   「이거 좀 대신 풀어봐」가 오가는 것 자체가 이 게임의 자리다.
# ※ 그래도 셋이 다 태워버리면 그 수수께끼는 죽는다. 구역 해금 두 장은 지켜봐야 한다.
PUZZLE_MAX_TRIES = 3


def _puzzle_open_now() -> tuple[bool, str]:
    """지금 수수께끼를 풀 수 있는 때인가.

    조사 페이즈에만 열어두면 「이번 턴에 못 풀면 다음 라운드까지 기다려라」가 되는데,
    그건 퍼즐이 아니라 대기다. 그래서 **판이 끝날 때까지 언제든** 풀 수 있게 두고,
    토론에서만 닫는다 — 토론은 서로 말로 맞춰 보는 자리이고, 그 시간에 각자 폰을
    들여다보며 답을 찍고 있으면 토론이 아니게 된다.
    """
    ph = SC.phase_by_seq(ROOM["seq"]) or {}
    if ph.get("key") == "talk":
        return False, "토론 중에는 수수께끼를 풀 수 없습니다 — 지금은 서로 맞춰 보는 시간입니다"
    return True, ""


@app.get("/api/puzzle/{role_id}")
def puzzle_list(role_id: str, clientId: str = ""):
    """그 배역이 지금 시도할 수 있는 수수께끼들. **답은 절대 안 나갑니다.**

    물음(prompt)은 이미 공개 카탈로그에 있는 몫이고, 여기서 더해 나가는 것은
    「내가 몇 번 틀렸는가」와, 세 번 넘게 틀린 사람에게만 열리는 힌트 한 줄입니다.
    """
    r = ROOM["roles"].get(role_id)
    if not r or r["clientId"] != clientId:
        return JSONResponse({"error": "권한 없음"}, status_code=403)
    ok, why = _puzzle_open_now()
    cur = current_round(ROOM["seq"])
    tries = ROOM.get("puzzleTries", {}).get(role_id, {})
    held = set()
    for cids in ROOM["hands"].values():
        held.update(cids)
    held.update(ROOM["revealed"])
    out = []
    for c in SC.CARDS:
        p = c.get("puzzle")
        if not p or c["id"] in held or c["round"] > cur:
            continue
        if _zone_lock(c.get("loc", ""), cur):
            continue
        # 선행이 아직 안 밝혀진 자리는 목록에 안 올린다 — 물음만 보이고 답은 안 받는
        # 자리가 되면, 못 푸는 것을 계속 붙들고 있게 된다.
        if [r for r in _card_needs(c) if r not in held]:
            continue
        n = int(tries.get(c["id"], 0))
        row = {"id": c["id"], "spot": c.get("spot", ""), "locName": c.get("locName", ""),
               "prompt": p.get("prompt", ""), "tries": n,
               # 세 번 틀린 자리는 목록에서 «지우지 않고» 잠긴 채로 둔다. 사라지면
               # 「내가 태웠다」는 사실까지 같이 사라져서, 옆사람에게 넘길 생각을 못 한다.
               "locked": n >= PUZZLE_MAX_TRIES,
               # 무엇이 나오는지는 미리 안 말한다 — 「도구가 나온다」까지만.
               "gives": bool(_is_gear(c) or _is_gear(SC.get_card(p.get("grants") or "") or {}))}
        if n >= PUZZLE_HINT_AFTER:
            row["hint"] = SC.puzzle_hint(c["id"]) if hasattr(SC, "puzzle_hint") else p.get("hint", "")
        out.append(row)
    return {"open": ok, "why": why, "hintAfter": PUZZLE_HINT_AFTER,
            "maxTries": PUZZLE_MAX_TRIES, "items": out}


@app.post("/api/puzzle")
def puzzle_answer(b: PuzzleTry):
    """수수께끼를 푼다. 맞히면 그 카드가 **푼 사람의 인벤토리로** 들어간다.

    조사턴을 안 쓴다 — 이 여섯 장은 조사 예산 밖의 물건이다.
    """
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId:
            return JSONResponse({"error": "그 배역으로 풀 수 없습니다"}, status_code=403)
        ok, why = _puzzle_open_now()
        if not ok:
            return JSONResponse({"error": why}, status_code=409)
        c = SC.get_card(b.cardId)
        if not c or not c.get("puzzle"):
            return JSONResponse({"error": "수수께끼가 없는 카드입니다"}, status_code=409)
        cur = current_round(ROOM["seq"])
        if c["round"] > cur:
            return JSONResponse({"error": f"아직 열리지 않았습니다 (조사 R{c['round']})"}, status_code=409)
        lock = _zone_lock(c.get("loc", ""), cur)
        if lock:
            return JSONResponse({"error": lock}, status_code=409)
        if _holder_of(b.cardId) or b.cardId in ROOM["revealed"]:
            return JSONResponse({"error": "이미 누군가 풀었습니다"}, status_code=409)

        tries = ROOM.setdefault("puzzleTries", {}).setdefault(b.roleId, {})
        # 세 번 틀린 사람은 이 카드 앞에 다시 못 선다. 답을 받기 «전에» 막아야
        # 네 번째 답이 우연히 맞아버리는 일이 없다.
        if int(tries.get(b.cardId, 0)) >= PUZZLE_MAX_TRIES:
            return JSONResponse(
                {"error": f"{PUZZLE_MAX_TRIES}번 틀려서 이 카드는 당신에게 잠겼습니다 — 다른 사람에게 넘기세요",
                 "locked": True, "tries": int(tries.get(b.cardId, 0))}, status_code=409)

        good = SC.check_puzzle(b.cardId, b.answer) if hasattr(SC, "check_puzzle") else False
        if not good:
            tries[b.cardId] = int(tries.get(b.cardId, 0)) + 1
            n = tries[b.cardId]
            out = {"ok": False, "tries": n, "locked": n >= PUZZLE_MAX_TRIES,
                   "maxTries": PUZZLE_MAX_TRIES}
            if n >= PUZZLE_HINT_AFTER:
                out["hint"] = SC.puzzle_hint(b.cardId) if hasattr(SC, "puzzle_hint") else ""
            bump()
            return out

        # ── 무엇이 나오는가 ────────────────────────────────────────────
        # 예전에는 수수께끼 카드 «자신» 이 푼 사람 손패로 들어갔다. 그래서 계기반
        # 눈금자를 풀면 손에 계기반 눈금자가 들어왔다 — 푼 보람이 없고, 손패 한 칸만
        # 먹는다. 수수께끼는 «자물쇠» 지 «상품» 이 아니다.
        #
        # 그래서 둘로 나눈다.
        #   ① 자물쇠(그 수수께끼 카드) — 풀린 자물쇠는 숨길 것이 없으니 테이블에 편다.
        #      무엇을 풀었는지는 셋이 다 본다.
        #   ② 상품(grants) — 푼 사람이 가져간다. 원고가 publish 를 달아 두었으면
        #      상품도 테이블로 간다(그 날 밤의 녹취처럼 다 같이 봐야 하는 것들).
        #
        # ★ 다만 **그 카드 자체가 도구인 경우는 예외다**(C1 회색조작기·C5 전기·
        #   H0 게임팩). 거기서는 자물쇠와 상품이 한 몸이라, 테이블에 펴 버리면
        #   아무도 그 도구를 «가진» 것이 안 되고 방탈출 조합이 성립하지 않는다.
        #
        # ★ **상품은 언제나 푼 사람 손패로 간다.** 원고의 `puzzle.publish` 는 여기서
        #   더 안 본다 — 풀어서 얻은 것이 곧바로 테이블에 펴지면 푼 사람이 쥐는 것이
        #   없고, 「내가 풀었으니 내가 들고 협상한다」가 사라진다.
        #   상품이 조사카드면 손패 상한을 그대로 받는다(넘치면 내려놓아야 한다).
        #   도구면 인벤토리로 가서 상한 밖이다 — 그 가름은 _split_hand 가 한다.
        pz = c.get("puzzle") or {}
        give = pz.get("grants") or ""
        if c.get("item"):
            err = _try_investigate(b.roleId, b.cardId, enforce_ap=False, _puzzle_bypass=True)
        else:
            _publish(b.cardId, by=b.roleId)
            err = None
        if not err and give and give != b.cardId:
            err = _try_investigate(b.roleId, give, enforce_ap=False, _puzzle_bypass=True)
        if err:
            return JSONResponse({"error": err}, status_code=409)
        # 구역이 열리는 수수께끼는 그 사실을 판에 알린다. 여태 조용히 열려서,
        # 지도를 다시 열어보기 전에는 새 구역이 생긴 줄 아무도 몰랐다.
        zone = c.get("unlockZone") or ""
        if zone:
            zn = next((z.get("name") for z in (getattr(SC, "MAP", []) or [])
                       if z.get("loc") == zone), "") or zone
            ROOM["table"].append({"kind": "system", "broadcast": True,
                                  "text": f"— 「{zn}」 구역이 열렸습니다."})
        give = give or b.cardId
        # 조사턴을 안 쓴다. _try_investigate 는 열어준 카드에 «이번 라운드» 도장을 찍는데,
        # 남은 조사 수는 그 도장을 세어 구한다 — 그대로 두면 수수께끼가 조사턴을 먹는다.
        # 묶음 형제들과 같은 방식으로 0라운드로 눕힌다.
        for _x in {b.cardId, give}:
            ROOM["checkedRound"].setdefault(b.roleId, {})[_x] = 0
        # 「누가 무엇을 풀었는가」는 공개 정보다 — 도구를 몇 개 모았는지가 곧 판의 시계다.
        # 다만 «무엇이 나왔는지» 는 푼 사람만 안다. 여기서는 물건 이름을 안 적는다.
        who = (SC.get_character(b.roleId) or {}).get("name", b.roleId)
        gc = SC.get_card(give) or {}
        # 도구는 이름을 부른다 — 누가 무엇을 몇 개 모았는지가 이 판의 시계다.
        # 도구가 아닌 것(녹취처럼 테이블에 펴지는 카드)은 이름을 안 부른다.
        got = gc.get("itemName") or ("무언가" if not pz.get("publish") else "")
        with _drip():
            if pz.get("publish"):
                ROOM["table"].append({"kind": "system", "broadcast": True,
                    "text": f"{who}{_subj(who)} {c.get('locName','')}의 수수께끼를 풀었다 — "
                            f"「{gc.get('title','')}」{_subj(gc.get('title',''))} 테이블에 펼쳐졌다."})
            else:
                ROOM["table"].append({"kind": "system",
                    "text": f"{who}{_subj(who)} {c.get('locName','')}의 수수께끼를 풀고 "
                            f"«{got}»{_obj(got)} 손에 넣었다."})
        bump()
    return {"ok": True, "card": SC.public_card(b.cardId)}


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
    """이 배역이 코드를 넣었는가. 좌석이 전부 사람이라 제 손으로 넣는 수밖에 없다."""
    return bool(ROOM["podCode"].get(rid))


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
    """이 막에서 제 칸을 잃는 사람 — 종막에서 표가 제일 많이 몰린 배역.

    동률이면 아무도 잃지 않는다. 「누군가는 반드시 잃는다」로 만들면 표가 갈렸을 때
    임의로 한 명을 골라야 하는데, 그건 판정이 아니라 주사위다.

    ★ 시나리오가 켤 때만 건다(DECISION_BAR_ACCUSED = True).
      이건 「결재」 기믹에서 온 규칙이다 — 범인으로 지목된 사람은 도장을 못 찍고,
      그 칸은 남은 사람들이 표로 채운다. 마지막 막이 «각자 한 문장씩 남기는 질문지»인
      사건에서는 이 규칙이 안 맞는다. 지목당했다고 자기 답을 못 적을 이유가 없고,
      셋이 각자 답해야 엔딩이 갈린다. 그래서 기본값은 «안 건다» 다.
    """
    if not getattr(SC, "DECISION_BAR_ACCUSED", False):
        return ""
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
    """자리마다 한 문장씩 고르는 막. 이 기믹이 있는 사건에서만 내려간다.

    무엇으로 불리는지는 사건이 정한다 — 「결재」인 사건도 있고 「마지막 질문」인 사건도
    있다. 그래서 화면에 뜰 이름(title)도 여기서 같이 내보낸다. 시나리오가 안 주면
    막 이름을 쓴다.

    행선지와 달리 «가리지» 않는다 — 앞사람이 무엇을 골랐는지 보고 따를지 뒤집을지
    정하는 자리라서 순서가 곧 규칙이다.
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
          # 이 막의 이름. 시나리오가 안 주면 클라이언트가 막 이름으로 폴백한다.
          "title": getattr(SC, "DECISION_TITLE", "") or "",
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
    assigned = [rid for rid, r in ROOM["roles"].items() if r["mode"] == "human"]
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
    assigned = [rid for rid, r in ROOM["roles"].items() if r["mode"] == "human"]
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
        if ROOM.get("host") is not None and not _host_ok(b.clientId, b.key):
            return JSONResponse({"error": "host"}, status_code=403)
        _night_resolve()
        return {"ok": True, "night": _night_public()}



# ── 질문지 — 고갯짓 둘로만 답이 오는 막 ──────────────────────────
def _ask_conf():
    return getattr(SC, "ASK_SHEET", None)


def _ask_order() -> list:
    return [rid for rid in SC.__dict__.get("CHARACTERS", [])] if False else [
        c["id"] for c in SC.CHARACTERS
        if (ROOM["roles"].get(c["id"]) or {}).get("mode") == "human"]


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
    """질문지 차례를 다음 사람에게 넘긴다."""
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


def _accuse1_warn() -> None:
    """지목 막에 들어설 때, 무엇이 걸려 있는지 «미리» 알린다.

    위험을 알고도 이름을 부르는 것이 압박이고, 모르고 당하면 그냥 사고다. 알고
    있으면 게임을 내려놓지 않는다 — 그래서 압수는 지목 «전에» 공지한다.

    공개 범위도 같이 못 박는다. **소지품까지만이고 손패(조사카드)는 안 열린다** —
    다 열리면 지목 한 번에 남은 목표가 통째로 0이 되고 이후가 소화경기가 된다.
    소지품을 안 쓰는 사건에서는 이 줄이 아예 안 나간다.
    """
    if not (getattr(SC, "BELONGINGS", None) or {}):
        return
    txt = getattr(SC, "BELONGINGS_WARN", "") or (
        "— 1차 범인지목입니다. 표를 가장 많이 받은 사람은 그 자리에서 «소지품»을 압수당합니다.\n"
        "    압수된 소지품은 모두가 봅니다. 손패(조사카드)는 열리지 않습니다.")
    ROOM["table"].append({"kind": "system", "broadcast": True, "text": txt})


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
                # 그리고 그 빈자리에 다른 것이 들어온다. 대화창에는 한 줄도 안 남는다.
                _decide_dev_from_accuse()
                _dev_fire_common_cut()
        bump()
    return {"ok": True, "accuse1": _accuse1_public()}


# ══════════════════════════════════════════════════════════════════
#  개발자 — 1차 범인지목의 대가
# ══════════════════════════════════════════════════════════════════
# 마을이 누군가를 범인으로 지목하면 그 자리가 legacy 로 표시되고, 안에 있던 것이
# 지워진다. **빈 몸에 개발자가 들어온다.** 돌아올 명분이 필요 없다 — 나간 적이
# 없고, 몸은 계속 마을에 있고 다른 것이 들어왔을 뿐이다.
#
# 아무도 못 밀어낸 판(동표)이나, 지울 자아가 없는 자리(플레이어)가 최다 득표인
# 판에서는 **들어올 빈 몸이 없다 — 개발자는 안 들어온다.** 그것도 정상적인 판이다.
#
# 이 칸은 그 사람 본인 말고는 아무에게도 안 나간다. 「누가 개발자인가」는 물론
# **「개발자가 들어왔는가」조차** 남의 화면에 실리면 안 된다 — 그게 새면 질문지
# Q2(「있느냐 없느냐부터」)가 통째로 무의미해진다. 그래서 여기서는 대화창에도
# 안 적고, 진행석이 읽는 사건 기록(_ev)에도 안 남긴다.
#
# 시나리오가 `DEV_FROM_ACCUSE = True` 를 적을 때만 돈다.
def _dev_on() -> bool:
    return bool(getattr(SC, "DEV_FROM_ACCUSE", False))


def _dev_pool() -> list:
    """개발자가 «들어갈 수 있는» 자리.

    시나리오가 `DEV_PICK["pool"]` 로 적어 둔다. 안 적었으면 좌석 전부로 본다 —
    누가 제외되는지는 사건의 사정이지 엔진이 알 일이 아니다.
    """
    dp = getattr(SC, "DEV_PICK", {}) or {}
    pool = [r for r in (dp.get("pool") or []) if r in ROOM["roles"]]
    return pool or list(ROOM["roles"])


def _accuse1_tally() -> dict:
    tally: dict[str, int] = {}
    for t in ((ROOM.get("accuse1") or {}).get("picks") or {}).values():
        tally[t] = tally.get(t, 0) + 1
    return tally


def _decide_dev_from_accuse() -> None:
    """1차 범인지목의 결과로 개발자를 정한다. 한 판에 한 번만.

    최다 득표자가 `pool` 안이면 그 사람이 개발자가 된다.
    표가 갈리면(동표) 판이 아무도 못 밀어낸 것이므로 아무도 안 들어온다.
    최다 득표자가 `pool` 밖이면 — 지울 자아가 없는 자리다 — 역시 안 들어온다.
    """
    if not _dev_on():
        return
    d = ROOM.setdefault("dev", {"decided": False, "id": "", "why": ""})
    if d.get("decided"):
        return
    tally = _accuse1_tally()
    if not tally:
        return                     # 표가 하나도 없으면 아직 아무 일도 안 일어났다
    top = max(tally.values())
    lead = sorted(t for t, v in tally.items() if v == top)
    pool = _dev_pool()
    d["decided"] = True
    d["seq"] = ROOM.get("seq")
    if len(lead) != 1:             # 1:1:1 — 판이 못 정하면 빈 몸도 안 생긴다
        d["id"], d["why"] = "", "tie"
    elif lead[0] not in pool:      # 지울 자아가 없는 자리
        d["id"], d["why"] = "", "empty"
    else:
        d["id"], d["why"] = lead[0], "top"


def _dev_id() -> str:
    """이 판의 개발자. 없으면 빈 문자열. **응답에 그대로 실으면 안 된다.**"""
    d = ROOM.get("dev") or {}
    return d.get("id", "") if (_dev_on() and d.get("decided")) else ""


def _dev_me(role_id: str) -> dict | None:
    """«그 사람 본인에게만» 가는 몫.

    개발자가 아닌 사람에게는 None 이다 — 「아니다」라는 답조차 안 나간다.
    """
    if not _dev_on() or not role_id or _dev_id() != role_id:
        return None
    dp = getattr(SC, "DEV_PICK", {}) or {}
    out = {"me": True, "title": dp.get("title", "관리자 모드"),
           "note": dp.get("note", "")}
    sheet = dp.get("sheet") or {}
    if sheet:
        out["sheet"] = sheet          # 롤카드가 갈아 끼워진다(기존 목표는 사라진다)
    return out


def _dev_my_cuts(role_id: str) -> list:
    """개발자가 된 사람만 보는 컷 — `myCuts` 로 나간다.

    방이 다 같이 보는 컷 목록에는 못 넣는다. 대신 «같은 id 의 공통 컷을 갈아 끼우는»
    자리라, 원고가 `DEV_PICK["cutAll"]` 로 **모두가 보는 컷**을 하나 두면 그 자리에
    이것이 덮인다 — 그러면 화면만 보고는 어느 갈래인지 알 수 없다.
    """
    if not _dev_me(role_id):
        return []
    dp = getattr(SC, "DEV_PICK", {}) or {}
    key = dp.get("cut") or "dev"
    fn = getattr(SC, "event_cut", None)
    if not fn:
        return []
    try:
        cuts = list(fn(key) or [])
    except Exception:                                   # noqa: BLE001
        cuts = []
    if not cuts:
        return []
    return [{"id": dp.get("cutAll") or key, "cuts": cuts}]


def _dev_fire_common_cut() -> None:
    """개발자 개입 막의 **공통** 컷. 개발자가 들어왔든 안 들어왔든 «똑같이» 튼다.

    들어온 판에서만 틀면 그 사실만으로 전원에게 공지가 된다(§7-h). 그래서 이 컷은
    지목이 끝난 자리에서 결과와 상관없이 한 번 돈다. 원고가 `DEV_PICK["cutAll"]` 을
    안 적었으면 아무 일도 안 일어난다.
    """
    key = (getattr(SC, "DEV_PICK", {}) or {}).get("cutAll")
    if key:
        _fire_cut(key)


# ══════════════════════════════════════════════════════════════════
#  방탈출 — 세상의 끝
# ══════════════════════════════════════════════════════════════════
# 열쇠 반쪽 둘이 «누구든» 인벤토리에 모이면 저절로 열린다. 열려도 아직 잠겨 있고,
# 열쇠를 쥔 사람이 «장착»해야 풀린다. 그 뒤가 퍼즐이다 — 앞은 «도구를 고르는» 문제,
# 마지막은 «어느 자리에 넣는가» 문제. 틀리면 문이 닫히고 판은 원래대로 돌아간다.
#
# 문안·정답은 전부 시나리오(`ESCAPE`)가 준다. 없는 사건에서는 이 화면이 아예 안 뜬다.
# **정답은 어떤 응답에도 안 실린다** — 화면에 나가는 것은 물음과 고를 것뿐이다.
def _escape_conf():
    return getattr(SC, "ESCAPE", None)


def _escape_keys() -> list:
    """입장권 — 열쇠 반쪽들. 도구가 아니라 문을 «보이게» 하는 물건이라 따로 센다.

    시나리오가 `ESCAPE["keys"]` 로 적어 두면 그것이 정본이다. 안 적었으면
    카드의 `keyHalf` 표를 보고, 그것도 없으면 도구 이름에 「열쇠」가 든 것을 쓴다.
    """
    conf = _escape_conf() or {}
    ks = [c for c in (getattr(SC, "KEY_HALVES", []) or []) if SC.get_card(c)]
    if ks:
        return ks
    ks = [c for c in (conf.get("keys") or []) if SC.get_card(c)]
    if ks:
        return ks
    ks = [c["id"] for c in SC.CARDS if c.get("keyHalf") or c.get("keyPart")]
    if ks:
        return ks
    return [c["id"] for c in SC.CARDS if c.get("item") and "열쇠" in str(c.get("itemName") or "")]


def _inv_all() -> dict:
    """지금 «누구의» 인벤토리에 무엇이 있는가. {카드id: 배역id}"""
    out = {}
    for rid in ROOM["roles"]:
        for cid in _inventory(rid):
            out[cid] = rid
    return out


def _escape_state() -> dict:
    return ROOM.setdefault("escape", {"open": False, "stage": "", "equipped": [], "step": 0,
                                      "log": [], "done": None, "fails": 0, "placed": {}})


def _escape_phase_seq() -> int:
    """방탈출 막이 몇 번째 막인가. 없으면 0.

    이 막은 **판의 고정 자리**에 있다 — 조건을 못 맞추면 통째로 지나가고 다음 막으로
    이어진다. 성공도 실패도 건너뜀도 전부 그 다음 막으로 이어진다(종착점이 아니다).
    """
    for p in getattr(SC, "PHASES", []) or []:
        if p.get("key") == "escape":
            return int(p.get("seq", 0) or 0)
    return 0


def _escape_keys_ready() -> bool:
    """열쇠 반쪽이 «누구든» 인벤토리에 다 모였는가."""
    keys = _escape_keys()
    if not keys:
        return False
    inv = _inv_all()
    return all(k in inv for k in keys)


def _phase_gate_ok(ph: dict) -> bool:
    """그 막이 열릴 조건을 갖췄는가.

    막에 `skipUnless` 가 붙어 있으면 그것이 정본이다 —
    `{"inventoryHas": [카드id…], "need": "all"|"any"|숫자, "why": "…"}`.
    적힌 카드가 **누구의 것이든** 인벤토리에 있으면 조건을 채운 것으로 본다.
    조건을 못 채운 막은 **통째로 지나간다** — 막 이름조차 안 뜬다.

    조건이 안 적힌 막은 언제나 열린다. 다만 방탈출 막만은, 조건을 안 적었어도
    열쇠 반쪽이 모여야 열린다(그게 그 막의 뜻이다).
    """
    cond = ph.get("skipUnless") or {}
    ids = [c for c in (cond.get("inventoryHas") or [])]
    if not ids:
        return _escape_keys_ready() if ph.get("key") == "escape" else True
    inv = _inv_all()
    have = [c for c in ids if c in inv]
    need = cond.get("need", "all")
    if isinstance(need, bool):
        need = "all"
    if isinstance(need, int):
        return len(have) >= max(0, need)
    return bool(have) if str(need) == "any" else len(have) == len(ids)


def _escape_open(force: bool = False) -> bool:
    """문을 연다(아직 잠긴 채로). 열쇠가 모자라면 False — 그 막은 건너뛴다."""
    conf = _escape_conf()
    if not conf:
        return False
    esc = _escape_state()
    if esc.get("stage"):                       # 이미 열렸거나, 풀렸거나, 닫혔다
        return bool(esc.get("open"))
    if not (force or _escape_keys_ready()):
        return False
    esc["open"] = True
    esc["stage"] = "locked"
    with _drip():
        ROOM["table"].append({"kind": "system", "broadcast": True,
                              "text": conf.get("locked") or "세상의 끝... 이 보인다. 하지만 잠겨 있다."})
        ROOM["table"].append({"kind": "system", "broadcast": True,
                              "text": "    열쇠를 쥔 사람이 그것을 «장착»해야 열립니다."})
    _ev("escape", state="locked")
    return True


def _escape_try_open() -> None:
    """방탈출 막이 아직 «없는» 사건에서만 도는 폴백.

    막이 `PHASES` 에 자리를 잡으면 그 막에 들어설 때만 열린다(위 `_escape_open`).
    아직 그 막이 안 붙은 원고에서는 열쇠 둘이 모이는 순간 열어 준다 — 그래야
    시나리오가 막을 얹기 전에도 이 기능을 굴려 볼 수 있다.
    """
    if _escape_phase_seq():
        return
    _escape_open()


def _escape_shut_on_leave() -> None:
    """방탈출 막을 떠난다. 못 풀고 나가면 문은 그냥 닫힌다 — 판은 다음 막으로 이어진다."""
    esc = _escape_state()
    if esc.get("stage") not in ("locked", "steps"):
        return
    conf = _escape_conf() or {}
    esc["open"] = False
    esc["stage"] = "closed"
    esc["done"] = False
    ROOM["table"].append({"kind": "system", "broadcast": True,
                          "text": conf.get("shut") or "문은 열리지 않았다. 그 자리가 다시 닫힌다."})
    _ev("escape", state="closed")


def _escape_steps() -> list:
    """이 문의 문제들. **답이 들어 있으므로 그대로 내보내면 안 된다.**

    시나리오가 `ESCAPE["steps"]` 를 주면 그것이 정본이다. 아직 없으면
    `slots` 와 `answer` 로 뼈대를 세운다 — 앞의 자리들은 「여기에 무엇을 꽂는가」
    (도구를 고르는 문제)이고, 마지막은 「남은 하나를 어느 자리에 넣는가」다.
    """
    conf = _escape_conf() or {}
    out = []
    for i, s in enumerate(conf.get("steps") or []):
        out.append({"id": s.get("id") or f"q{i+1}", "kind": s.get("kind") or "tool",
                    "prompt": s.get("prompt", ""), "note": s.get("note", ""),
                    "answer": s.get("answer")})
    if out:
        return out
    slots = list(conf.get("slots") or [])
    ans = dict(conf.get("answer") or {})
    if not slots or not ans:
        return []
    for s in slots[:-1]:
        out.append({"id": s.get("id", ""), "kind": "tool",
                    "prompt": f'「{s.get("label") or s.get("id")}」 — 여기에 무엇을 꽂는가?',
                    "note": s.get("note", ""), "answer": ans.get(s.get("id", ""), "")})
    last = slots[-1]
    out.append({"id": last.get("id", ""), "kind": "slot",
                "prompt": "남은 것은 하나다 — 어느 자리에 넣는가?",
                "note": "", "answer": last.get("id", "")})
    code = conf.get("code") or {}
    if code.get("require"):
        out.append({"id": "code", "kind": "code", "prompt": code.get("prompt", ""),
                    "note": "", "answer": code.get("answer")})
    return out


def _escape_tool_options() -> list:
    """지금 낼 수 있는 도구들. 열쇠 반쪽과 이미 제자리에 꽂힌 것은 뺀다.

    「누가 도구를 몇 개 모았는가」는 원래 공개 정보다. 문 앞에서는 무엇을 낼 수
    있는지도 같이 봐야 고를 수 있으므로, 이 자리에서만 이름이 열린다.
    """
    esc = _escape_state()
    keys = set(_escape_keys())
    used = set((esc.get("placed") or {}).values())
    out = []
    for cid, rid in sorted(_inv_all().items()):
        if cid in keys or cid in used:
            continue
        c = SC.get_card(cid) or {}
        out.append({"id": cid, "name": c.get("itemName") or c.get("title", cid), "holder": rid})
    return out


def _escape_public(role_id: str = "") -> dict | None:
    """방탈출 화면 몫. **정답은 한 톨도 안 실린다.**"""
    conf = _escape_conf()
    if not conf:
        return None
    esc = _escape_state()
    if not esc.get("stage"):
        return None                       # 아직 열리지 않았다 — 화면 자체가 안 뜬다
    steps = _escape_steps()
    keys = _escape_keys()
    inv = _inv_all()
    out = {"stage": esc.get("stage"), "open": bool(esc.get("open")),
           "done": esc.get("done"), "fails": int(esc.get("fails", 0)),
           "intro": conf.get("intro", ""),
           "locked": conf.get("locked") or "세상의 끝... 이 보인다. 하지만 잠겨 있다.",
           "slots": [{"id": s.get("id", ""), "label": s.get("label", ""), "note": s.get("note", "")}
                     for s in (conf.get("slots") or [])],
           "keys": [{"id": k, "name": (SC.get_card(k) or {}).get("itemName", ""),
                     "equipped": k in [e.get("cardId") for e in (esc.get("equipped") or [])]}
                    for k in keys],
           "myKeys": [k for k in keys if role_id and inv.get(k) == role_id],
           "placed": [{"slot": sid, "name": (SC.get_card(cid) or {}).get("itemName", "")}
                      for sid, cid in (esc.get("placed") or {}).items()],
           "log": list(esc.get("log") or [])[-8:],
           "total": len(steps)}
    if esc.get("stage") == "steps" and 0 <= esc.get("step", 0) < len(steps):
        st = steps[esc["step"]]
        row = {"n": esc["step"] + 1, "total": len(steps), "kind": st["kind"],
               "prompt": st.get("prompt", ""), "note": st.get("note", "")}
        if st["kind"] == "tool":
            row["options"] = _escape_tool_options()
        elif st["kind"] == "slot":
            row["options"] = [{"id": s.get("id", ""), "label": s.get("label", ""),
                               "note": s.get("note", "")} for s in (conf.get("slots") or [])]
        else:
            row["options"] = []            # 코드는 적어 넣는다
        out["step"] = row
    if esc.get("done"):
        # 클리어 정보 — 여기서부터는 전원에게 열린다.
        out["clear"] = conf.get("clear") or conf.get("after") or conf.get("ok", "")
        out["bonus"] = int(conf.get("bonus", 5))
        goals = conf.get("goals") or {}
        if role_id and goals.get(role_id):
            out["mineGoal"] = goals[role_id]      # 이 한 줄만 그 사람 몫이다
    if esc.get("done") is False:
        out["shut"] = conf.get("no", "문이 닫혔다.")
    return out


def _add_score(role_id: str, key: str, pts: int) -> None:
    """자동으로 셀 수 있는 가점만 쌓는다."""
    if not role_id:
        return
    row = ROOM.setdefault("scores", {}).setdefault(role_id, {})
    row[key] = int(row.get(key, 0)) + int(pts)


def _escape_finish(ok: bool) -> None:
    conf = _escape_conf() or {}
    esc = _escape_state()
    esc["open"] = False
    if not ok:
        # 문이 닫힌다. 판은 원래대로 돌아간다 — 여기서 막을 되돌릴 것은 없다.
        esc["stage"] = "closed"
        esc["done"] = False
        with _drip():
            ROOM["table"].append({"kind": "system", "broadcast": True,
                                  "text": conf.get("no") or "무언가가 제자리에 있지 않다. 문이 닫힌다."})
        _ev("escape", state="closed")
        return
    esc["stage"] = "done"
    esc["done"] = True
    bonus = int(conf.get("bonus", 5))
    for rid in (_human_roles() or list(ROOM["roles"])):
        _add_score(rid, "escape", bonus)
    with _drip():
        ROOM["table"].append({"kind": "system", "broadcast": True,
                              "text": conf.get("ok") or "문이 열린다."})
        ROOM["table"].append({"kind": "system", "broadcast": True,
                              "text": f"— 문이 열렸습니다. 모두에게 {bonus}점이 더해집니다.\n"
                                      "    「내 정보 · 추가 정보」가 하나 늘었습니다."})
    _fire_cut("escape:ok")
    _ev("escape", state="done")


# ══════════════════════════════════════════════════════════════════
#  질문지 — 선지형 · 채점
# ══════════════════════════════════════════════════════════════════
# 「누가 범인인가」 하나만 물으면 개발자 축이 안 잡힌다. 선지로 묻고, 서로의 답이
# 서로의 점수를 움직이게 한다 — 그래야 종막까지 서로를 못 놓는다.
#
# 원고의 `FINAL_QUESTIONS` 가 **선지형**(dict + options)이면 이 기능이 켜지고,
# 옛 서술형(문자열 목록)이면 아무 일도 안 일어난다 — 예전 사건은 그대로 돈다.
#
#   {"id","q","options":[{"k","t"}],"correct":[선지키…],"bonus":{선지키: 점수},
#    "points": 배점, "draft": True,
#    "correctIsDev": True,   그 판의 «실제» 개발자 배역이 정답이 된다
#    "noDevKey": "none",     개발자가 안 들어온 판에서는 이 선지가 정답
#    "hitsDev": True}        개발자 감점을 세는 문항이 어느 것인가
#
# 개발자의 점수는 자기 답으로 안 만들어진다 — **다른 둘의 합**이 자기 점수이고,
# **자기를 개발자로 맞힌 사람 수 × 20 을 뺀다**(비율이 아니라 고정 감점).
# 그 셈의 값은 `SCORE_RULES["dev"]` 가 정한다.
#
# 원고가 이름을 어떻게 붙이든 읽는다 — 물음은 `q·text·t`, 선지는 `choices·options·opts`,
# 선지 하나는 `{"id"|"k", "label"|"t"|"name"}` 이거나 그냥 문자열이다. 화면 쪽과
# 원고 쪽이 서로 다른 이름을 쓰고 있어서, 엔진이 둘 다 받아 준다.
_QUIZ_SECRETS = ("correct", "bonus", "correctIsDev", "noDevKey", "hitsDev", "answer")


def _quiz_opts(q: dict) -> list:
    for k in ("choices", "options", "opts"):
        if q.get(k):
            return list(q[k])
    return []


def _quiz_opt(o) -> dict:
    if not isinstance(o, dict):
        return {"k": str(o), "t": str(o)}
    key = o.get("id") or o.get("k") or ""
    return {"k": str(key), "t": str(o.get("label") or o.get("t") or o.get("name") or key)}


def _quiz_text(q: dict) -> str:
    return str(q.get("q") or q.get("text") or q.get("t") or "")


def _quiz_questions() -> list:
    return [q for q in (getattr(SC, "FINAL_QUESTIONS", []) or [])
            if isinstance(q, dict) and _quiz_opts(q)]


def _quiz_on() -> bool:
    return bool(_quiz_questions())


def _quiz_askable() -> list:
    """답을 «받아야» 하는 문항의 id. 자리표시자(draft)는 세지 않는다."""
    return [str(q.get("id") or "") for q in _quiz_sheet() if q.get("id")]


def _quiz_qid(q: dict, i: int) -> str:
    return str(q.get("id") or f"q{i + 1}")


def _quiz_sheet() -> list:
    """화면에 나가는 몫. **정답과 판정 키는 한 톨도 안 나간다.**

    원고가 `final_questions_public()` 을 내주면 그것이 정본이다(자리표시자 문항은
    거기서 이미 빠져 있고, **답안 배열의 순서도 그 결과의 순서**다). 안 내주는
    사건에서는 서버가 같은 일을 손으로 한다 — 정답 키를 걷어내고 물음과 선지만 남긴다.
    """
    fn = getattr(SC, "final_questions_public", None)
    if fn:
        try:
            return [dict(q) for q in (fn() or [])]
        except Exception:                       # noqa: BLE001 — 질문지 하나 때문에 판이 멈추면 안 된다
            pass
    out = []
    for i, q in enumerate(_quiz_questions()):
        if q.get("draft"):
            continue
        out.append({"id": _quiz_qid(q, i), "q": _quiz_text(q), "note": q.get("note", ""),
                    "options": [{"id": o["k"], "label": o["t"]} for o in
                                (_quiz_opt(x) for x in _quiz_opts(q))]})
    return out


def _quiz_optmap() -> dict:
    """`{문항id: {선지id}}` — 올라온 답이 실제로 있는 선지인지 보는 데만 쓴다."""
    out = {}
    for q in _quiz_sheet():
        opts = [_quiz_opt(o) for o in _quiz_opts(q)]
        out[str(q.get("id") or "")] = {o["k"] for o in opts}
    return out


def _quiz_correct(q: dict) -> set:
    """그 문항의 정답 선지들. **이 값은 서버 밖으로 안 나간다.**

    `correctIsDev` 가 붙은 문항은 정답이 판마다 다르다 — 개발자가 «들어온» 판에서는
    그 배역이, 안 들어온 판에서는 `noDevKey` 선지가 정답이다.
    """
    ok = {k for k in (q.get("correct") or [])}
    if q.get("correctIsDev"):
        dev = _dev_id()
        if dev:
            ok.add(dev)
        elif q.get("noDevKey"):
            ok.add(q["noDevKey"])
    return ok


def _final_conf() -> dict:
    return dict(getattr(SC, "FINAL_SHEET", {}) or {})


def _quiz_show_seq() -> int:
    """질문지를 «보여주기» 시작하는 막(`FINAL_SHEET["revealSeq"]`).

    종막에만 있으면 무엇을 알아내야 하는지 모른 채 조사가 끝난다. 원고가 안 정했으면
    방탈출 막(없으면 종막)부터 보여준다.
    """
    n = int(_final_conf().get("revealSeq") or getattr(SC, "QUIZ_SHOW_SEQ", 0) or 0)
    if n:
        return n
    n = _escape_phase_seq()
    if n:
        return n
    return _quiz_answer_seq()


def _quiz_answer_seq() -> int:
    """답을 «받는» 막(`FINAL_SHEET["answerSeq"]`). 안 정했으면 종막(`final`)이다."""
    n = int(_final_conf().get("answerSeq") or 0)
    if n:
        return n
    for p in getattr(SC, "PHASES", []) or []:
        if p.get("key") == "final":
            return int(p.get("seq", 0) or 0)
    return 0


def _quiz_answering() -> bool:
    """지금이 답을 확정하는 자리인가. 그 전에는 질문지를 «읽기만» 한다(§7-g)."""
    n = _quiz_answer_seq()
    ph = SC.phase_by_seq(ROOM["seq"]) or {}
    return bool(n) and ROOM["seq"] >= n and ph.get("key") != "reveal"


def _quiz_notice() -> None:
    """질문지가 놓이는 자리에서 한 번. 「내 점수가 남의 답에 깎인다」를 여기서 공표한다."""
    q = ROOM.setdefault("quiz", {"open": False, "answers": {}, "scored": False, "result": None})
    if q.get("open") or not _quiz_on():
        return
    q["open"] = True
    conf = _final_conf()
    txt = conf.get("intro") or getattr(SC, "QUIZ_NOTICE", "") or (
        "질문지를 놓습니다. 지금은 «읽기만» 합니다 — 답은 종막에서 확정합니다.")
    ROOM["table"].append({"kind": "system", "broadcast": True,
                          "text": f'— {conf.get("title") or "질문지"} — {txt}'})
    rule = conf.get("rule") or (
        "★ 이 판에는 «남의 답에 내 점수가 깎이는» 문항이 있습니다."
        if ((getattr(SC, "SCORE_RULES", {}) or {}).get("dev")) else "")
    if rule:
        ROOM["table"].append({"kind": "system", "broadcast": True, "text": rule})


def _quiz_points_self() -> tuple:
    """원고가 채점 함수를 안 내주는 사건용 — 서버가 직접 센다.

    돌려주는 것: (`{배역: 점수}`, `{문항: [맞힌 배역…]}`, 감점 문항 id)
    """
    qs = _quiz_questions()
    dconf = (getattr(SC, "SCORE_RULES", {}) or {}).get("dev") or {}
    hit_q = dconf.get("hit_from") or next(
        (_quiz_qid(q, i) for i, q in enumerate(qs) if q.get("hitsDev")), "")
    answers = (ROOM.get("quiz") or {}).get("answers") or {}
    seats = _human_roles() or list(ROOM["roles"])
    pts, hits = {}, {}
    for rid in seats:
        picks = answers.get(rid) or {}
        got = 0
        for i, q in enumerate(qs):
            if q.get("draft"):
                continue
            qid = _quiz_qid(q, i)
            k = picks.get(qid, "")
            if k and k in _quiz_correct(q):
                got += int(q.get("points", 0) or 0)
                hits.setdefault(qid, []).append(rid)
            got += int((q.get("bonus") or {}).get(k, 0) or 0)
        pts[rid] = got
    return pts, hits, hit_q


def _quiz_grade() -> dict | None:
    """채점.

    정답도 배점도 원고의 것이다 — 원고가 `score_final_answers(answers, dev_id)` 를
    내주면 그것이 정본이고, 서버는 **개발자의 밑돈**만 얹는다. 그 밑돈은 질문지만으로
    못 센다 — 「다른 두 사람의 «최종» 점수 합」이라 방이 쥔 다른 가점까지 세야 한다.
    """
    if not _quiz_on():
        return None
    dev = _dev_id()
    answers = (ROOM.get("quiz") or {}).get("answers") or {}
    seats = _human_roles() or list(ROOM["roles"])
    sr = getattr(SC, "SCORE_RULES", {}) or {}
    dconf = sr.get("dev") or {}
    fn = getattr(SC, "score_final_answers", None)
    res = None
    if fn:
        try:
            res = fn({r: dict(answers.get(r) or {}) for r in seats}, dev_id=dev)
        except Exception:                       # noqa: BLE001
            res = None
    if res:
        pts = dict(res.get("points") or {})
        hits = dict(res.get("hits") or {})
        dsc = res.get("devScore") or {}
        pen = int(dsc.get("penalty", 0) or 0)
        floor = dsc.get("floor", dconf.get("floor"))
        hit_q = dconf.get("hit_from", "")
        nhit = int(res.get("devHits", 0) or 0)
    else:
        pts, hits, hit_q = _quiz_points_self()
        per = abs(int(dconf.get("penalty_per_hit", -20) or -20))
        nhit = len([r for r in (hits.get(hit_q) or []) if r != dev]) if (dev and hit_q) else 0
        pen, floor = -per * nhit, dconf.get("floor")
    rows = {}
    for rid in seats:
        # 방이 따로 쌓아 둔 가점(방탈출 등)도 최종 점수에 든다.
        extra = sum(int(v) for v in (ROOM.get("scores", {}).get(rid) or {}).values())
        own = int(pts.get(rid, 0) or 0)
        rows[rid] = {"roleId": rid, "name": _person_name(rid), "quiz": own,
                     "extra": extra, "total": own + extra, "dev": False,
                     "picks": dict(answers.get(rid) or {})}
    if dev and dev in rows:
        # 개발자는 자기 점수를 못 얻는다 — 다른 둘의 «최종» 합이 밑돈이고,
        # 거기서 자기를 맞힌 사람 수만큼 «고정»으로 깎인다.
        others = [r for r in rows if r != dev]
        base = sum(rows[r]["total"] for r in others)
        total = base + pen
        if floor is not None:
            total = max(int(floor), total)
        rows[dev].update({"dev": True, "quiz": 0, "base": base, "hits": nhit,
                          "penalty": pen, "total": total,
                          "formula": dconf.get("formulaKo", "")})
    return {"rows": [rows[r] for r in seats if r in rows], "devId": dev,
            "noDev": not dev, "hitQ": hit_q, "hits": hits}


def _quiz_all_in() -> bool:
    need = _quiz_askable()
    answers = (ROOM.get("quiz") or {}).get("answers") or {}
    seats = _human_roles()
    if not seats or not need:
        return False
    return all(all((answers.get(r) or {}).get(q) for q in need) for r in seats)


def _quiz_finish() -> None:
    """채점한다. 결과는 «진상 공개»에서만 열린다 — 그 전에 열면 개발자가 그 자리에서 드러난다."""
    q = ROOM.setdefault("quiz", {"open": False, "answers": {}, "scored": False, "result": None})
    if q.get("scored") or not _quiz_on():
        return
    q["scored"] = True
    q["result"] = _quiz_grade()
    q["open"] = False
    ROOM["table"].append({"kind": "system", "broadcast": True,
                          "text": "— 질문지가 걷혔습니다. 채점은 엔딩에서 함께 봅니다."})
    _ev("quiz", state="scored")


def _final_sheet() -> dict | None:
    """「질문지가 놓였다」 한 칸. 공개 = **읽기만** 되고 답은 아직 안 받는다(§7-g).

    답을 받는 막(`FINAL_SHEET["answerSeq"]`)에 들어서면 이 칸이 닫힌다 —
    화면이 그때부터 답을 받는 쪽으로 그린다.
    """
    if not _quiz_on():
        return None
    conf = _final_conf()
    show = _quiz_show_seq()
    open_ = bool(show and ROOM["seq"] >= show) and not _quiz_answering()
    out = {"open": open_}
    for k in ("title", "intro", "rule"):
        if conf.get(k):
            out[k] = conf[k]
    return out


def _quiz_public(role_id: str = "") -> dict | None:
    """질문지 화면 몫. 정답도, 남의 답도, 채점 결과도 때가 되기 전에는 안 나간다."""
    if not _quiz_on():
        return None
    ph = SC.phase_by_seq(ROOM["seq"]) or {}
    q = ROOM.get("quiz") or {}
    show = bool(q.get("open") or q.get("scored") or ROOM["seq"] >= _quiz_show_seq())
    if not show:
        return None
    conf = _final_conf()
    out = {"open": _quiz_answering() and not q.get("scored"),
           "shown": True, "scored": bool(q.get("scored")),
           "title": conf.get("title") or getattr(SC, "QUIZ_TITLE", "질문지"),
           "note": conf.get("intro") or getattr(SC, "QUIZ_NOTE", ""),
           "rule": conf.get("rule", ""),
           "questions": _quiz_sheet(),
           "answered": sorted((q.get("answers") or {}).keys()),
           "voters": len(_human_roles())}
    if role_id:
        out["mine"] = dict((q.get("answers") or {}).get(role_id) or {})
    if ph.get("key") == "reveal" and q.get("scored"):
        out["result"] = q.get("result")            # 여기서만 열린다
    return out


def _quiz_from_list(answers: list) -> dict:
    """화면이 보내는 «순서대로의 문자열 배열»을 선지 표로 옮긴다.

    옛 서술 답과 같은 창구(`/api/final-answers`)로 올라오기 때문에, 번호가
    `FINAL_QUESTIONS` 의 자리와 맞는다. 값은 선지 id 가 원칙이지만 이름으로 와도
    받아 준다 — 화면 쪽과 원고 쪽이 서로 다른 이름을 쓰고 있어서다.
    """
    picks = {}
    for i, q in enumerate(_quiz_sheet()):
        qid = str(q.get("id") or "")
        v = str((answers[i] if i < len(answers) else "") or "").strip()
        if not (qid and v):
            continue
        opts = [_quiz_opt(o) for o in _quiz_opts(q)]
        hit = next((o["k"] for o in opts if v in (o["k"], o["t"])), "")
        if hit:
            picks[qid] = hit
    return picks


class QuizPick(BaseModel):
    roleId: str
    clientId: str
    answers: dict = {}        # {문항id: 선지키}


@app.post("/api/quiz")
def quiz_pick(b: QuizPick):
    """질문지의 답을 낸다. 종막에서만 받는다 — 그 전에는 «읽기만» 하는 것이 이 판의 규칙이다."""
    with LOCK:
        if not _quiz_on():
            return JSONResponse({"error": "이 사건에는 선지형 질문지가 없습니다"}, status_code=404)
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "그 배역으로 답할 수 없습니다"}, status_code=403)
        if not _quiz_answering():
            return JSONResponse({"error": "종막에서 확정합니다"}, status_code=409)
        q = ROOM.setdefault("quiz", {"open": False, "answers": {}, "scored": False, "result": None})
        if q.get("scored"):
            return JSONResponse({"error": "이미 걷혔습니다"}, status_code=409)
        ok = _quiz_optmap()
        picks = {}
        for qid, k in (b.answers or {}).items():
            if qid not in ok:
                return JSONResponse({"error": f"없는 문항입니다 — {qid}"}, status_code=400)
            if k and k not in ok[qid]:
                return JSONResponse({"error": f"없는 선지입니다 — {qid}"}, status_code=400)
            if k:
                picks[qid] = k
        q.setdefault("answers", {}).setdefault(b.roleId, {}).update(picks)
        # 다 냈으면 그 자리에서 걷는다. 결과는 엔딩에서 함께 본다.
        if _quiz_all_in():
            _quiz_finish()
        bump()
    return {"ok": True, "quiz": _quiz_public(b.roleId)}


class EscapeAct(BaseModel):
    roleId: str
    clientId: str
    cardId: str = ""          # 장착할 열쇠 반쪽
    pick: str = ""            # 이번 문제의 답(도구 id · 자리 id · 적어 넣은 코드)


@app.post("/api/escape/equip")
def escape_equip(b: EscapeAct):
    """열쇠 반쪽을 문틀에 맞춘다. 반쪽이 다 맞물리면 잠긴 것이 풀린다."""
    with LOCK:
        conf = _escape_conf()
        if not conf:
            return JSONResponse({"error": "이 사건에는 그 문이 없습니다"}, status_code=404)
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "그 배역으로 할 수 없습니다"}, status_code=403)
        esc = _escape_state()
        if esc.get("stage") != "locked":
            return JSONResponse({"error": "지금 열쇠를 낼 자리가 아닙니다"}, status_code=409)
        keys = _escape_keys()
        if b.cardId not in keys:
            return JSONResponse({"error": "그건 이 문의 열쇠가 아닙니다"}, status_code=409)
        if b.cardId not in _inventory(b.roleId):
            return JSONResponse({"error": "그 열쇠는 당신에게 없습니다"}, status_code=403)
        done = [e.get("cardId") for e in (esc.get("equipped") or [])]
        if b.cardId not in done:
            esc.setdefault("equipped", []).append({"cardId": b.cardId, "roleId": b.roleId})
            nm = (SC.get_card(b.cardId) or {}).get("itemName", "열쇠")
            who = _person_name(b.roleId)
            with _drip():
                ROOM["table"].append({"kind": "system", "broadcast": True,
                                      "text": f"{who}{_subj(who)} «{nm}»{_obj(nm)} 문틀에 맞췄다."})
        if all(k in [e.get("cardId") for e in esc["equipped"]] for k in keys):
            esc["stage"] = "steps"
            esc["step"] = 0
            with _drip():
                ROOM["table"].append({"kind": "system", "broadcast": True,
                                      "text": conf.get("unlocked") or "반쪽 둘이 맞물린다 — 잠긴 것이 풀렸다."})
                if conf.get("intro"):
                    ROOM["table"].append({"kind": "system", "broadcast": True, "text": conf["intro"]})
            _ev("escape", state="steps")
            if not _escape_steps():
                # 아직 문제가 안 적힌 원고 — 열쇠만으로 열리는 문이 된다.
                # 답할 것도 없는데 화면이 「답하시오」로 멈춰 서 있으면 판이 거기서 끝난다.
                _escape_finish(True)
        bump()
    return {"ok": True, "escape": _escape_public(b.roleId)}


@app.post("/api/escape/answer")
def escape_answer(b: EscapeAct):
    """이번 문제에 답한다. 틀리면 그 자리에서 문이 닫힌다.

    도구를 고르는 문제는 «자기 인벤토리에 있는 것»만 낼 수 있다 — 없는 것을 꽂을 수는 없다.
    """
    with LOCK:
        conf = _escape_conf()
        if not conf:
            return JSONResponse({"error": "이 사건에는 그 문이 없습니다"}, status_code=404)
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId or r["mode"] != "human":
            return JSONResponse({"error": "그 배역으로 할 수 없습니다"}, status_code=403)
        esc = _escape_state()
        if esc.get("stage") != "steps":
            return JSONResponse({"error": "지금 답할 자리가 아닙니다"}, status_code=409)
        steps = _escape_steps()
        i = int(esc.get("step", 0))
        if not steps or i >= len(steps):
            return JSONResponse({"error": "답할 문제가 없습니다"}, status_code=409)
        st = steps[i]
        pick = (b.pick or "").strip()
        if not pick:
            return JSONResponse({"error": "고르지 않았습니다"}, status_code=400)
        if st["kind"] == "tool":
            # 고를 것은 인벤토리에서 뽑아 보여주지만, 막는 것은 «손에 있는가»까지만 본다.
            # 원고가 도구 표시(item)를 아직 안 붙인 카드를 정답으로 적어두면, 조건을
            # 인벤토리로 잠글 때 문이 영영 안 열리는 자리가 생긴다.
            if pick not in (ROOM["hands"].get(b.roleId) or []):
                return JSONResponse({"error": "그건 당신이 쥔 것이 아닙니다"}, status_code=403)
            if pick in set((esc.get("placed") or {}).values()):
                return JSONResponse({"error": "이미 꽂혀 있습니다"}, status_code=409)
        ans = st.get("answer")
        good = (pick in [str(a) for a in ans]) if isinstance(ans, (list, tuple, set)) \
            else (pick == str(ans or ""))
        who = _person_name(b.roleId)
        if st["kind"] == "tool":
            nm = (SC.get_card(pick) or {}).get("itemName", "무언가")
            line = f"{who}{_subj(who)} «{nm}»{_obj(nm)} 꽂았다."
        elif st["kind"] == "slot":
            lb = next((s.get("label", pick) for s in (conf.get("slots") or []) if s.get("id") == pick), pick)
            line = f"{who}{_subj(who)} 남은 것을 「{lb}」에 넣었다."
        else:
            line = f"{who}{_subj(who)} 코드를 눌렀다."
        esc.setdefault("log", []).append(line)
        with _drip():
            ROOM["table"].append({"kind": "system", "broadcast": True, "text": line})
        if not good:
            fails = int(esc.get("fails", 0)) + 1
            esc["fails"] = fails
            if fails >= int(conf.get("tries", 1) or 1):
                _escape_finish(False)
                bump()
                return {"ok": False, "escape": _escape_public(b.roleId)}
            with _drip():
                ROOM["table"].append({"kind": "system", "broadcast": True,
                                      "text": conf.get("miss") or "들어가지 않는다. 다시 골라야 한다."})
            bump()
            return {"ok": False, "escape": _escape_public(b.roleId)}
        if st["kind"] == "tool":
            esc.setdefault("placed", {})[st["id"]] = pick
        elif st["kind"] == "slot":
            # 남은 하나가 그 자리에 들어갔다. 무엇이 남았는지는 인벤토리가 안다.
            left = [o["id"] for o in _escape_tool_options()]
            esc.setdefault("placed", {})[pick] = left[0] if len(left) == 1 else ""
        esc["step"] = i + 1
        if esc["step"] >= len(steps):
            _escape_finish(True)
        bump()
    return {"ok": True, "escape": _escape_public(b.roleId)}


def _auto_sweep_one(role_id: str, do_puzzles: bool) -> list:
    """그 배역이 «지금 할 수 있는 조사»를 알아서 다 한다. 무슨 일이 있었는지 되돌려준다.

    QA 전용이다. 3인 판을 혼자 검수할 때 한 사람이 스물한 번을 손으로 눌러야 조사
    페이즈가 끝나는데, 그걸 다 누르고 나면 정작 보려던 다음 막까지 못 간다.

    ★ 순서가 있다. **수수께끼를 먼저 푼다** — 구역을 여는 수수께끼(F1·D4)가 풀려야
      하늘 끝·바다 끝의 카드가 후보에 들어온다. 뒤에 풀면 그 구역은 이번 라운드를
      통째로 건너뛴다.
    ★ 조사턴(ap)은 그대로 쓴다. 자동이라고 예산을 무시하면 검수한 것이 실제 판이
      아니게 된다 — 「몇 장까지 열리는가」가 이 게임의 뼈대다.
    """
    log = []
    cur = current_round(ROOM["seq"])
    if do_puzzles:
        # 한 번 풀면 구역이 열리고 후보가 늘어난다. 더 못 풀 때까지 돈다.
        for _ in range(len(getattr(SC, "CARDS", []) or [])):
            did = False
            held = set(ROOM["revealed"])
            for cids in ROOM["hands"].values():
                held.update(cids)
            for c in SC.CARDS:
                p = c.get("puzzle")
                if not p or c["id"] in held or c.get("round", 1) > cur:
                    continue
                if _zone_lock(c.get("loc", ""), cur):
                    continue
                if [r for r in _card_needs(c) if r not in held]:
                    continue
                ans = (p.get("answer") or [""])[0]
                if not SC.check_puzzle(c["id"], ans):
                    log.append(f"{c['id']} 수수께끼 — 정답이 안 맞습니다(원고 확인 필요)")
                    continue
                give = p.get("grants") or ""
                if c.get("item"):
                    err = _try_investigate(role_id, c["id"], enforce_ap=False, _puzzle_bypass=True)
                else:
                    _publish(c["id"], by=role_id)
                    err = None
                if not err and give and give != c["id"]:
                    err = _try_investigate(role_id, give, enforce_ap=False, _puzzle_bypass=True)
                if err:
                    log.append(f"{c['id']} 수수께끼 — {err}")
                    continue
                for _x in {c["id"], give or c["id"]}:
                    ROOM["checkedRound"].setdefault(role_id, {})[_x] = 0
                zone = c.get("unlockZone") or ""
                if zone:
                    zn = next((z.get("name") for z in (getattr(SC, "MAP", []) or [])
                               if z.get("loc") == zone), "") or zone
                    ROOM["table"].append({"kind": "system", "broadcast": True,
                                          "text": f"— 「{zn}」 구역이 열렸습니다."})
                log.append(f"{c['id']} 「{c.get('title', '')}」 풀었습니다"
                           + (f" → {give}" if give and give != c['id'] else ""))
                did = True
                break
            if not did:
                break
    # 남은 조사턴을 쓴다. 후보가 라운드 안에서 계속 줄어드니 한 장씩 다시 고른다.
    guard = 0
    while guard < 40:
        guard += 1
        left = _ap_for(ROOM["seq"]) - _round_checks(role_id, cur)
        if left <= 0:
            break
        cands = _openable_cards(role_id)
        if not cands:
            log.append("더 열 자리가 없습니다")
            break
        pick = cands[0]["id"]
        err = _try_investigate(role_id, pick)
        if err:
            log.append(f"{pick} — {err}")
            break
        log.append(f"{pick} 「{cands[0].get('title', '')}」 조사")
    return log


@app.post("/api/qa/auto")
def qa_auto(b: AutoSweep):
    """QA 자동조사. **열쇠가 있어야 한다** — 평소 판에서는 존재하지도 않는 길이다."""
    if not _agent_ok(b.key):
        return JSONResponse({"error": "key"}, status_code=403)
    with LOCK:
        ph = SC.phase_by_seq(ROOM["seq"]) or {}
        if ph.get("key") != "invest":
            return JSONResponse({"error": "조사 페이즈에서만 씁니다"}, status_code=409)
        ids = [b.roleId] if b.roleId else [rid for rid, r in ROOM["roles"].items()
                                           if r.get("mode") == "human"]
        out = {}
        for rid in ids:
            if rid not in ROOM["roles"]:
                out[rid] = ["없는 배역"]
                continue
            out[rid] = _auto_sweep_one(rid, b.puzzles)
        bump()
    return {"ok": True, "log": out}


@app.get("/api/hand/{role_id}")
def get_hand(role_id: str, clientId: str = ""):
    with LOCK:
        r = ROOM["roles"].get(role_id)
        if not r or r["clientId"] != clientId:  # 엄격: 내 손패만
            return JSONResponse({"error": "권한 없음"}, status_code=403)
        mine = ROOM["hands"].get(role_id, [])
        return {"hand": [SC.public_card(c) for c in mine],
                "notes": _my_notes(role_id, mine)}


# ── 에이전트(코드 세션) 원격 조종: 진행석 읽기·마킹 ──
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
            "finalAnswers": ROOM["finalAnswers"],
        }


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
            allowed = allowed or _host_ok(b.clientId, b.key)
        else:
            allowed = True
        if not allowed:
            return JSONResponse({"error": "권한 없음"}, status_code=403)
        if _crisis_blocking():
            return JSONResponse({"error": "침수 대응이 먼저입니다"}, status_code=409)
        _advance_turn()
        return {"ok": True, "turn": ROOM.get("turn")}


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


@app.get("/api/handoff", response_class=PlainTextResponse)
def handoff_brief(key: str = "", base: str = ""):
    """진행 세션이 스스로 받아 가는 지침. 배포된 코드에서 만들어지므로 낡을 일이 없다.

    저장소를 진행 세션에 주지 않기로 한 이상, 지침을 사람이 복사해 나르면 판마다
    조금씩 어긋난다. 이 주소 하나만 알려주면 그 문제가 없어진다.
    진상은 들어 있지 않다.
    """
    if not _agent_ok(key):
        return PlainTextResponse("key", status_code=403)
    return handoff.runner_brief(SC, base or "", key or "<AGENT_KEY>")


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
    if ROOM.get("host") is not None and not _host_ok(b.clientId, b.key):
        return JSONResponse({"error": "host"}, status_code=403)
    with LOCK:
        _crisis_resolve()
        ROOM["flood"] = _flood_for(ROOM["seq"])
        bump()
    return {"ok": True, "solved": (ROOM.get("crisis") or {}).get("solved")}


@app.post("/api/advance")
def advance(b: HostReq):
    # 호스트가 지정돼 있으면 호스트나 GM 진행석(열쇠)만, 없으면 누구나(현행 앱 호환)
    if ROOM.get("host") is not None and not _host_ok(b.clientId, b.key):
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
        if (ROOM["roles"].get(rid) or {}).get("mode") != "human":
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
            # 표가 다 모였으면 그 결과로 개발자가 정해진다. 아무에게도 안 알린다 —
            # 본인 화면에만 뜬다(_dev_me).
            _decide_dev_from_accuse()
            _dev_fire_common_cut()
        # 방탈출 막을 떠난다면, 못 푼 문은 여기서 닫힌다.
        if SC.phase_by_seq(ROOM["seq"]).get("key") == "escape":
            _escape_shut_on_leave()
        # 답을 받는 막을 떠나면 질문지는 그 자리에서 걷힌다 — 한 사람이 안 냈어도 셈은 선다.
        if _quiz_on() and _quiz_answering():
            _quiz_finish()
        if ROOM["seq"] < len(SC.PHASES):
            ROOM["seq"] += 1
            # 방탈출 막은 고정 자리에 있지만 «조건이 안 차면 통째로 지나간다».
            # 열쇠 반쪽이 아무의 인벤토리에도 없으면 그 막은 아예 안 열리고
            # 바로 다음 막(3차 조사)으로 이어진다 — 막 이름도 안 뜬다.
            while (ROOM["seq"] < len(SC.PHASES)
                   and not _phase_gate_ok(SC.phase_by_seq(ROOM["seq"]))):
                _sk = SC.phase_by_seq(ROOM["seq"])
                _ev("phase_skipped", name=_sk.get("name", ""), key=_sk.get("key", ""))
                ROOM["seq"] += 1
            seq = ROOM["seq"]
            ph = SC.phase_by_seq(seq)
            _reveal_autos()          # 이 막에 스스로 열리는 자리부터 편다
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
            if ph.get("key") == "escape":
                # 여기까지 왔다는 것은 그 막의 조건을 채웠다는 뜻이다(위에서 걸렀다).
                _escape_open(force=True)
            if ph.get("key") == "accuse":
                _accuse1_warn()
            # 질문지는 종막 전에 «놓이기»만 한다. 무엇을 알아내야 하는지 알고
            # 남은 막을 굴리라는 뜻이다(답은 종막에서 확정한다).
            if _quiz_show_seq() and seq >= _quiz_show_seq():
                _quiz_notice()
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


# ── 종막 — 서술 답변은 기록으로 남고, 엔딩은 지목표가 정한다 ──────
@app.post("/api/final-answers")
def final_answers(b: FinalAnswers):
    """종막 질문지의 서술 답변. 채점하지 않는다 — 그대로 보관한다.

    사람 셋이 하는 판이라 채점자가 없다. 서술은 진행석과 큰 화면에서 다 같이
    읽는 기록이고, 엔딩을 가르는 것은 그 옆의 지목표다(SC.compute_ending).
    """
    with LOCK:
        r = ROOM["roles"].get(b.roleId)
        if not r or r["clientId"] != b.clientId:
            return JSONResponse({"error": "그 배역의 답이 아닙니다"}, status_code=403)
        ROOM["finalAnswers"][b.roleId] = list(b.answers)
        # 선지형 문항이 섞여 있으면 그 몫은 채점으로 간다. 서술은 예전처럼 기록으로만 남는다.
        # 답이 «확정»되는 자리는 종막뿐이다 — 그 전에는 질문지를 읽기만 한다(§7-g).
        if _quiz_on() and _quiz_answering():
            q = ROOM.setdefault("quiz", {"open": False, "answers": {}, "scored": False, "result": None})
            if not q.get("scored"):
                picks = _quiz_from_list(list(b.answers))
                if picks:
                    q.setdefault("answers", {}).setdefault(b.roleId, {}).update(picks)
                    if _quiz_all_in():
                        _quiz_finish()
        bump()
    return {"ok": True, "answers": list(b.answers)}


@app.post("/api/reset")
def reset(b: HostReq):
    global ROOM
    with LOCK:
        if ROOM.get("host") is not None and not _host_ok(b.clientId, b.key):
            return JSONResponse({"error": "host"}, status_code=403)
        ROOM = fresh_room()
        # 호스트도 함께 푼다. 붙들고 있으면 그 브라우저가 사라졌을 때 방이 영영 잠긴다 —
        # 초기화한 사람은 곧바로 다시 잡으면 된다(클라이언트가 이어서 요청한다).
    return {"ok": True}


# 화면은 매번 다시 물어보게 한다. ETag 만 보내고 Cache-Control 을 안 주면 브라우저가
# 제멋대로 「아직 신선하다」고 판단해 옛 파일을 그대로 쓴다 — 고친 게 안 나온다는 신고의 대부분이 이것이었다.
# no-cache 는 「쓰지 마라」가 아니라 「쓰기 전에 물어봐라」다. 안 바뀌었으면 304로 값싸게 끝난다.
_NOCACHE = {"Cache-Control": "no-cache, must-revalidate"}


# 「고친 게 왜 안 보이지」에 화면이 스스로 답하게 한다. 저장소만 봐서는 배포가
# 실제로 붙었는지 알 수 없다 — 서버가 자기가 어떤 판인지 말해야 한다.
# Render 는 RENDER_GIT_COMMIT 을 넣어주고, 로컬은 .git 에서 읽는다.
def _build_stamp() -> dict:
    sha = os.getenv("RENDER_GIT_COMMIT") or ""
    if not sha:
        try:
            head = (_HERE / ".git" / "HEAD").read_text().strip()
            sha = (_HERE / ".git" / head[5:]).read_text().strip() if head.startswith("ref: ") else head
        except Exception:
            sha = ""
    try:
        at = time.strftime("%y-%m-%d %H:%M", time.gmtime((_HERE / "server.py").stat().st_mtime))
    except Exception:
        at = ""
    return {"sha": sha[:7] or "local", "at": at}


_BUILD = _build_stamp()


@app.get("/api/build")
def api_build():
    return _BUILD


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
    ip = lan_ip()
    print("=" * 56)
    print(f"  GAME DAY · {SC.TITLE} — 사람 셋이서 하는 머더미스터리")
    print("  브라우저에서 열기:")
    print(f"    이 컴퓨터    →  http://127.0.0.1:{PORT}")
    print(f"    같은 와이파이 →  http://{ip}:{PORT}   (폰·다른 PC는 이 주소로)")
    print("=" * 56)
    uvicorn.run(app, host=HOST, port=PORT)
