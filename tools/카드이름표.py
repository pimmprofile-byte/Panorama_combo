#!/usr/bin/env python3
"""카드를 부르는 이름 세 가지를 한 표로 뽑는다.

    python3 tools/카드이름표.py rule_the_day          # 화면에 찍기
    python3 tools/카드이름표.py rule_the_day --write  # 문서의 표를 갈아 끼우기

카드 하나에는 사람이 부를 수 있는 이름이 **세 개** 있고, 셋이 다 다른 것을
가리킵니다. 헷갈리면 대화가 통째로 어긋나므로 부르는 법을 하나로 못박습니다 —
자세한 것은 `docs/카드_부르는_법.md` 를 보세요.

    위치 · 뒷면이름 · 카드이름

문서 쪽 표는 손으로 고치지 마세요. 원고(`scenarios/<사건>.py`)를 고친 뒤
`--write` 로 다시 뽑으면 됩니다.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

BEGIN = "<!-- 이름표 시작 · tools/카드이름표.py 가 씁니다 — 손으로 고치지 마세요 -->"
END = "<!-- 이름표 끝 -->"


def rows(mod):
    """지도 차례 → 조사 차수 → 원고 차례 로 줄을 세운다."""
    cards = list(getattr(mod, "CARDS", []))
    order = {z["loc"]: i for i, z in enumerate(getattr(mod, "MAP", []))}
    at = {c["id"]: i for i, c in enumerate(cards)}
    return sorted(cards, key=lambda c: (order.get(c["loc"], 99),
                                        c.get("round", 9), at[c["id"]]))


def table(mod) -> str:
    out = []
    here = None
    for c in rows(mod):
        if c["loc"] != here:
            here = c["loc"]
            out.append("")
            out.append(f"### {c.get('locName') or here}")
            out.append("")
            out.append("| ID | 차수 | 뒷면이름 (조사 핀) | 카드이름 (열었을 때) |")
            out.append("|---|---|---|---|")
        same = " ⚠" if (c.get("spot") or "") == (c.get("title") or "") else ""
        out.append(f"| {c['id']} | {c.get('round', '?')}차 | "
                   f"{c.get('spot') or '—'}{same} | {c.get('title') or '—'} |")
    out.append("")
    return "\n".join(out).strip() + "\n"


def main(sid: str, write: bool) -> int:
    import scenarios
    mod = scenarios.get(sid)
    if mod is None:
        print(f"그런 사건이 없습니다: {sid}")
        return 1

    body = table(mod)
    dup = [c["id"] for c in getattr(mod, "CARDS", [])
           if (c.get("spot") or "") == (c.get("title") or "")]

    if not write:
        print(body)
        if dup:
            print(f"⚠ 뒷면이름과 카드이름이 같은 카드: {' · '.join(dup)}")
        return 0

    doc = HERE / "docs" / "카드_부르는_법.md"
    if not doc.exists():
        print(f"문서가 없습니다: {doc}")
        return 1
    text = doc.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        print("문서에 이름표 자리(주석 두 줄)가 없습니다.")
        return 1
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    doc.write_text(f"{head}{BEGIN}\n\n{body}\n{END}{tail}", encoding="utf-8")
    print(f"{doc} · {len(getattr(mod, 'CARDS', []))}장 새로 썼습니다.")
    if dup:
        print(f"⚠ 뒷면이름과 카드이름이 같은 카드: {' · '.join(dup)}")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print(__doc__)
        raise SystemExit(1)
    raise SystemExit(main(args[0], "--write" in sys.argv))
