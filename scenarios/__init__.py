"""시나리오 레지스트리 — 여러 사건을 id로 등록/조회한다.

각 사건 모듈은 같은 인터페이스를 노출한다 (ID/META/TITLE/CHARACTERS/CARDS/…,
public_scenario()/compute_ending()/build_*_prompt()/…). 무엇이 필수이고
무엇이 «있으면 켜지는 것»인지는 `docs/시나리오_인터페이스.md` 에 있다.

새 사건을 붙이는 방법:
  1. `scenarios/template.py` 를 복사해 `scenarios/<id>.py` 로 만든다
  2. 아래 세 자리에 이름을 넣는다 — 임포트, _MODS, _ORDER
  3. 끝. 로비(landing.html)와 관리자 화면이 저절로 집어간다
"""
# 이 패키지는 사건 둘로 이루어진다 — 같은 세계(콤보-룰더데이)의 두 게임이다.
# 둘째(combo)는 아직 원고가 없고, 포스터만 걸린 «잠긴 카드»로 뜬다(META.locked).
#
# template 은 «복사해 가는 틀»이라 목록에 안 올린다. 파일은 그대로 두되 여기서만 뺀다 —
# 로비에 빈 게임이 같이 뜨면 잘못 눌러 들어가는 일이 생긴다.
from . import rule_the_day, combo

_MODS = {rule_the_day.ID: rule_the_day, combo.ID: combo}
# 로비에 뜨는 순서. 첫 번째가 기본 사건이다.
_ORDER = [rule_the_day.ID, combo.ID]


def get(sid):
    return _MODS.get(sid) or _MODS[_ORDER[0]]


def ids():
    return list(_ORDER)


def default_id():
    return _ORDER[0]


# 사건 하나가 「읽을거리」로서 얼마나 무거운가. 난이도(추리의 어려움)와는 다른 축이라,
# 고르는 사람이 미리 알아야 할 정보다 — 20분짜리인 줄 알고 앉았다가 한 시간을 읽게 되면
# 그건 재미가 아니라 사고다. 원고를 고칠 때마다 저절로 따라오게 계산해서 낸다.
def _text_load(m) -> dict:
    """플레이어 한 명이 실제로 읽게 되는 글자 수. 남의 롤카드는 안 읽으므로 1인분만 센다."""
    role = 0
    for c in getattr(m, "CHARACTERS", []):
        for k in ("hook", "storyPast", "storyToday", "tagline", "persona"):
            role += len(str(c.get(k) or ""))
        for sec in (c.get("sheet") or []):
            role += len(str(sec.get("h", ""))) + len(str(sec.get("b", "")))
        for x in (c.get("hide") or []) + (c.get("tips") or []):
            role += len(str(x))
        for r in (c.get("past") or []):
            role += len(str(r.get("w", ""))) + len(str(r.get("t", "")))
        for g in (c.get("goals") or []):
            role += len(str(g.get("t", "")))
    n = max(1, len(getattr(m, "CHARACTERS", []) or [1]))
    cards = sum(len(c.get("text", "")) + len(c.get("hint", "")) for c in getattr(m, "CARDS", []))
    common = len(str(getattr(m, "COMMON_INTRO", "") or "")) + len(str(getattr(m, "VICTIM", "") or ""))
    common += sum(len(str(x)) for x in (getattr(m, "ALIBI_LOG", []) or []))
    total = role // n + cards + common
    # 분당 500자 — 처음 보는 글을 곱씹으며 읽는 속도다(술술 읽는 속도의 절반쯤).
    mins = max(1, round(total / 500))
    if total < 5000:
        tier, label = 1, "가볍게"
    elif total < 9000:
        tier, label = 2, "보통"
    elif total < 15000:
        tier, label = 3, "읽을거리 많음"
    else:
        tier, label = 4, "장편"
    return {"chars": total, "minutes": mins, "tier": tier, "label": label,
            "bar": "▮" * tier + "▯" * (4 - tier)}


def meta_list():
    out = []
    for i in _ORDER:
        m = _MODS[i]
        row = {"id": m.ID, **m.META}
        # 아직 원고가 없는 준비중 사건은 0자로 뜨는 게 더 이상하다 — 아예 빼둔다.
        if not m.META.get("locked"):
            row["text"] = _text_load(m)
        out.append(row)
    return out
