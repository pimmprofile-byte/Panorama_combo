"""AI가 낀 판이 실제로 어떻게 굴러가는지 돌려본다.

모델은 부르지 않는다. 조사 순번·카드 선택·손패 정리·포드 투표·검거 판정은
전부 server.py의 진짜 함수를 그대로 쓴다 — 따로 흉내 낸 규칙이 있으면
시뮬레이션이 실제와 갈라져 아무 쓸모가 없어진다.

AI 선택은 완전히 결정적이라 그냥 돌리면 매번 같은 판이 나온다. 실제 게임의
변동은 두 곳에서 온다 — (1) 어느 자리에 사람이 앉는가, (2) 대화가 어느 구역으로
흘러 AI를 그리로 끌어당기는가. 그 둘을 흔들어 표본을 만든다.

    python3 tools/simulate.py [판수]
"""
from __future__ import annotations

import random
import sys
import zlib
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server as S                                    # noqa: E402
import scenarios                                      # noqa: E402


def setup(human_ids: list[str]) -> None:
    """새 방을 열고 자리를 채운다. human_ids에 없는 배역은 전부 AI."""
    S.ROOM.clear()
    S.ROOM.update(S.fresh_room())
    S.SC = scenarios.get("submarine")
    S.ROOM["roomId"] = "sim"
    for c in S.SC.CHARACTERS:
        S.ROOM["roles"][c["id"]] = {
            "mode": "human" if c["id"] in human_ids else "ai",
            "clientId": f'h:{c["id"]}' if c["id"] in human_ids else None,
        }
    S.ROOM["hands"] = {c["id"]: [] for c in S.SC.CHARACTERS}
    S.ROOM["started"] = True
    S.ROOM["seq"] = 1


def human_pick(rid: str, rng: random.Random) -> list[str]:
    """사람 자리는 이렇게 둔다 — 자기 목표 카드가 열려 있으면 먼저 집고,
    아니면 자기 구역과 「내가 아는 카드」 쪽을 뒤진다. 머미 플레이어의 기본형이다."""
    ap = S._ap_for(S.ROOM["seq"]) - S._round_checks(rid, S.current_round(S.ROOM["seq"]))
    prof = (getattr(S.SC, "INVEST_AI", {}) or {}).get(rid, {})
    goal = set((getattr(S.SC, "KEEP_GOALS", {}).get(rid) or {}).get("cards", []))
    home = set(prof.get("home", []))
    took = []
    for _ in range(max(0, ap)):
        cands = S._openable_cards(rid)
        if not cands:
            break
        want = [c for c in cands if c["id"] in goal] \
            or [c for c in cands if c["loc"] in home] \
            or cands
        pick = rng.choice(want[: max(1, len(want) // 2)] or want)
        if S._try_investigate(rid, pick["id"], enforce_turn=False):
            break
        took.append(pick["id"])
    S._ai_trim_hand(rid)
    return took


def chatter_drift(rng: random.Random) -> None:
    """대화가 어느 구역을 향하는지가 AI의 다음 조사를 끌어당긴다(_hot_locs).
    모델을 안 부르니 그 흐름을 구역 이름 한 줄로 대신 넣어준다."""
    loc = rng.choice([c["locName"] for c in S.SC.CHARACTERS and S.SC.CARDS])
    S.ROOM["table"].append({"kind": "ai", "roleId": "", "speaker": "", "text": f"{loc} 쪽을 봐야 하지 않을까요."})


def run_one(human_ids: list[str], seed: int) -> dict:
    rng = random.Random(seed)
    setup(human_ids)
    order0 = [c["id"] for c in S.SC.CHARACTERS]
    for rnd in (1, 2, 3):
        S.ROOM["seq"] = {1: 2, 2: 4, 3: 6}[rnd]
        S._reset_turn_for_seq(S.ROOM["seq"])
        for rid in S._turn_order() or order0:
            if S.ROOM["roles"][rid]["mode"] == "human":
                human_pick(rid, rng)
            else:
                S._ai_pick(rid, S._ap_for(S.ROOM["seq"]) - S._round_checks(rid, rnd))
                S._ai_trim_hand(rid)
        for _ in range(rng.randint(1, 3)):
            chatter_drift(rng)

    hands = {rid: list(cs) for rid, cs in S.ROOM["hands"].items()}
    revealed = list(S.ROOM["revealed"])

    # 보유 목표 — 게임이 끝난 시점에 그 카드가 아직 자기 손에 있는가
    keeps = {}
    for rid, kg in (getattr(S.SC, "KEEP_GOALS", {}) or {}).items():
        keeps[rid] = all(cid in hands.get(rid, []) for cid in kg["cards"])

    # 조합 — 두 장이 다 공개돼야 테이블에서 이야기가 된다
    pairs = [p["key"] for p in getattr(S.SC, "CARD_PAIRS", [])
             if all(c in revealed for c in p["cards"])]

    # 진범이 자기에게 불리한 것을 몇 장이나 덮었는가
    cul = getattr(S.SC, "CULPRIT_ID", "")
    culhide = (getattr(S.SC, "INVEST_AI", {}).get(cul) or {}).get("hide", [])
    buried = [c for c in culhide if c in hands.get(cul, [])]
    exposed = [c for c in culhide if c in revealed]

    # 진범을 가리키는 공개 카드가 몇 장 깔렸는가 = 사람들이 그를 의심할 근거
    susp = getattr(S.SC, "public_suspicion", lambda r: {})(revealed)

    return {"revealed": revealed, "hands": hands, "keeps": keeps, "pairs": pairs,
            "buried": buried, "exposed": exposed, "susp": susp,
            "unopened": [c["id"] for c in S.SC.CARDS if c["id"] not in revealed
                         and not any(c["id"] in h for h in hands.values())]}


def pod_and_arrest(human_ids: list[str]) -> tuple[dict, dict]:
    votes = dict(getattr(S.SC, "POD_VOTE_AI", {}))
    pod = S.SC.pod_result(votes)
    cul = getattr(S.SC, "CULPRIT_ID", "")
    need = {n: S.SC.arrest_needed(n, cul in human_ids) for n in (1, 2, 3, 4, 5, 6)}
    return pod, need


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    chars = {c["id"]: c["name"] for c in scenarios.get("submarine").CHARACTERS}
    ids = list(chars)

    print("=" * 66)
    print("① AI 여섯 — 아무도 사람이 앉지 않은 판 (완전 결정적, 한 번이면 끝)")
    print("=" * 66)
    r = run_one([], 0)
    print(f"공개 {len(r['revealed'])}장 · 손패에 묶인 채 끝 "
          f"{sum(len(h) for h in r['hands'].values())}장 · 아무도 안 연 것 {len(r['unopened'])}장")
    for rid, h in r["hands"].items():
        if h:
            print(f"  {chars[rid]:5s} 손패 {[S.SC.get_card(c)['title'] for c in h]}")
    print(f"\n  보유목표: " + " · ".join(
        f"{chars[k]} {'달성' if v else '실패'}" for k, v in r["keeps"].items()))
    print(f"  성립한 조합 {len(r['pairs'])}/{len(getattr(S.SC,'CARD_PAIRS',[]))}: {', '.join(r['pairs'])}")
    print(f"  진범이 덮은 카드 {len(r['buried'])}장 {[S.SC.get_card(c)['title'] for c in r['buried']]}")
    print(f"  진범이 놓쳐 공개된 카드 {len(r['exposed'])}장 {[S.SC.get_card(c)['title'] for c in r['exposed']]}")
    print(f"  공개 카드가 가리키는 사람: " + ", ".join(
        f"{chars[k]} {v}장" for k, v in sorted(r["susp"].items(), key=lambda x: -x[1])))

    print()
    print("=" * 66)
    print(f"② 사람이 섞인 판 — 자리 수·자리 배치·대화 흐름을 흔들어 {n}판")
    print("=" * 66)
    keep_ok = Counter()
    keep_n = Counter()
    pair_n = Counter()
    reveal_n = []
    buried_n = []
    susp_cul = []
    cul = getattr(S.SC, "CULPRIT_ID", "")
    by_humans = defaultdict(lambda: [0, 0])
    rng = random.Random(7)
    for i in range(n):
        hn = rng.randint(1, 4)
        humans = rng.sample(ids, hn)
        r = run_one(humans, i + 1)
        reveal_n.append(len(r["revealed"]))
        buried_n.append(len(r["buried"]))
        susp_cul.append(r["susp"].get(cul, 0))
        for k, v in r["keeps"].items():
            keep_n[k] += 1
            keep_ok[k] += bool(v)
            by_humans[(k, k in humans)][0] += 1
            by_humans[(k, k in humans)][1] += bool(v)
        for p in r["pairs"]:
            pair_n[p] += 1

    print(f"평균 공개 {sum(reveal_n)/n:.1f}장 / 43장  (최소 {min(reveal_n)} · 최대 {max(reveal_n)})")
    print(f"진범이 덮는 데 성공한 불리한 카드: 평균 {sum(buried_n)/n:.2f}장")
    print(f"공개 카드가 진범을 가리키는 장수: 평균 {sum(susp_cul)/n:.2f}장\n")

    print("보유목표 달성률")
    for k in keep_n:
        tot_h, ok_h = by_humans[(k, True)]
        tot_a, ok_a = by_humans[(k, False)]
        print(f"  {chars[k]:5s} 전체 {keep_ok[k]/keep_n[k]*100:5.1f}%"
              f"   | 사람이 앉았을 때 {ok_h/tot_h*100 if tot_h else 0:5.1f}%"
              f"   AI가 맡았을 때 {ok_a/tot_a*100 if tot_a else 0:5.1f}%")

    print("\n조합 성립률 (두 장이 다 공개돼야 테이블에서 말이 된다)")
    for p in getattr(S.SC, "CARD_PAIRS", []):
        c = pair_n.get(p["key"], 0)
        bar = "█" * round(c / n * 24)
        print(f"  {p['key']:12s} {c/n*100:5.1f}% {bar}")

    pod, need = pod_and_arrest([])
    print()
    print("=" * 66)
    print("③ 포드와 검거 — 규칙 자체가 정하는 것")
    print("=" * 66)
    seats = getattr(S.SC, "POD_SEATS", 2)
    print(f"AI 고정표만으로 굴렸을 때(정원 {seats}): "
          + (", ".join(chars[x] for x in pod.get("boarded", [])) or "아무도 못 탐"))
    print(f"  득표: " + ", ".join(f"{chars[k]} {v}" for k, v in sorted(
        Counter(getattr(S.SC, 'POD_VOTE_AI', {}).values()).items(), key=lambda x: -x[1])))
    print(f"\n진범({chars.get(cul,cul)})이 AI일 때 검거에 필요한 사람 표:")
    for hn in (1, 2, 3, 4, 5, 6):
        print(f"  사람 {hn}명 → {S.SC.arrest_needed(hn, False)}표 필요"
              f"   (진범을 사람이 맡았다면 {S.SC.arrest_needed(hn, True)}표)")


if __name__ == "__main__":
    main()
