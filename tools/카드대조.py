#!/usr/bin/env python3
"""조사카드가 «두 곳»에 적혀 있어서 서로 어긋나는 것을 잡는다.

    python3 tools/카드대조.py rule_the_day

기준은 언제나 원고 하나다 — `scenarios/<사건>.py` 의 CARDS.
`pending/<사건>/카드.md` 는 그 원고를 베껴 온 «그림 지시»이고, 여기서 어긋나면
문서 쪽이 틀린 것이다. 그래서 이 도구는 문서를 고치라고만 말하고, 원고는
건드리지 않는다.

무엇을 대조하는가 —
  · 카드가 빠졌는가 / 원고에 없는 항목이 있는가
  · 이름이 다른가
  · 몇 차 조사인지가 다른가(문서의 절 나눔)
  · 절 제목에 적힌 장수가 맞는가

어긋난 것이 하나라도 있으면 1 을 물려준다(작업 흐름에 걸어 두기 좋게).
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

CARD_RE = re.compile(r"^### (.+?) → `\w+_card_([A-Z0-9]+)\.webp`", re.M)
SEC_RE = re.compile(r"^# 3-(\d)\. (\d)차 조사 — (\d+)장", re.M)


def load_doc(path: Path):
    """문서에서 (카드ID → (이름, 차수)) 와 절 제목의 장수를 읽는다."""
    text = path.read_text(encoding="utf-8")
    secs = [(m.start(), int(m.group(2)), int(m.group(3))) for m in SEC_RE.finditer(text)]

    def round_at(pos: int):
        cur = None
        for start, rnd, _ in secs:
            if start < pos:
                cur = rnd
        return cur

    cards = {}
    for m in CARD_RE.finditer(text):
        cards[m.group(2)] = (m.group(1).strip(), round_at(m.start()))
    return cards, {rnd: n for _, rnd, n in secs}, text


def main(sid: str) -> int:
    doc_path = HERE / "pending" / sid / "카드.md"
    if not doc_path.exists():
        print(f"문서가 없습니다: {doc_path}")
        return 1

    import scenarios
    mod = scenarios.get(sid)
    if mod is None:
        print(f"그런 사건이 없습니다: {sid}")
        return 1
    live = {c["id"]: c for c in getattr(mod, "CARDS", [])}

    doc, sec_n, _ = load_doc(doc_path)
    bad = []

    for cid, c in live.items():
        if cid not in doc:
            bad.append(f"문서에 없는 카드 — {cid} R{c['round']} 「{c.get('title','')}」")
    for cid, (title, rnd) in doc.items():
        if cid not in live:
            bad.append(f"원고에 없는 항목 — {cid} 「{title}」 (문서 {rnd}차)")
            continue
        if live[cid].get("title") != title:
            bad.append(f"이름이 다름 — {cid}  문서「{title}」 ≠ 원고「{live[cid].get('title')}」")
        if rnd != live[cid]["round"]:
            bad.append(f"차수가 다름 — {cid} 「{title}」  문서 {rnd}차 ≠ 원고 {live[cid]['round']}차")

    want = {}
    for c in live.values():
        want[c["round"]] = want.get(c["round"], 0) + 1
    for rnd, n in sorted(sec_n.items()):
        if want.get(rnd, 0) != n:
            bad.append(f"절 제목의 장수가 다름 — {rnd}차  문서 {n}장 ≠ 원고 {want.get(rnd,0)}장")

    total = len(live)
    head = doc_path.read_text(encoding="utf-8").splitlines()[0]
    m = re.search(r"(\d+)장", head)
    if m and int(m.group(1)) != total:
        bad.append(f"머리말의 장수가 다름 — 문서 {m.group(1)}장 ≠ 원고 {total}장")

    print(f"원고 {total}장 · 문서 {len(doc)}장")
    if not bad:
        print("맞습니다 — 두 곳이 같습니다.")
        return 0
    print(f"\n어긋난 곳 {len(bad)}:")
    for line in bad:
        print("  ·", line)
    print("\n기준은 원고입니다 — 문서(pending/…/카드.md) 쪽을 고치세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "rule_the_day"))
