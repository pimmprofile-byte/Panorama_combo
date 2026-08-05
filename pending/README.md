# pending — 에셋 프롬프트 작업 폴더

구역 배경과 조사카드 그림을 만들기 위한 프롬프트가 여기 삽니다.
**서버가 이 마크다운을 그대로 읽어 관리자 화면에 뿌립니다** — 관리자 모드 →
「에셋 프롬프트」 탭. JSON 사본을 따로 두지 않는 이유가 그것입니다. 문서와
화면이 언젠가 갈라지느니 문서 하나를 정본으로 둡니다.

---

## 쓰는 순서

1. **`00_화풍_공통.md`** 를 먼저 읽습니다. 규격 · 앞머리 블록 · 리젝 규칙이 전부 여기 있습니다.
2. 사건 폴더의 `구역.md` · `카드.md` 를 엽니다.
3. **앞머리 블록 + 그 사건의 톤 블록 + 개별 본문** 을 이어 붙여 넣습니다.
   (관리자 화면의 「합쳐서 복사」가 이 셋을 한 번에 이어줍니다.)
4. 나온 그림은 그 폴더의 **`완성본/`** 에 파일명 그대로 넣습니다.
5. 확인이 끝난 것만 `assets/` 로 옮깁니다. 옮기는 순간 화면이 집어갑니다.

---

## 폴더

```
pending/
  README.md              ← 지금 이 파일
  00_화풍_공통.md         ← 화풍 · 규격 · 앞머리 · 리젝 규칙 (전 사건 공통)
  template_빈판/
    구역.md   카드.md   완성본/
```

사건을 하나 붙이면 여기에 폴더를 하나 만들고, **`server.py` 의 `_ASSET_SETS`
에 한 줄 적습니다.** 그래야 관리자 화면의 탭에 뜹니다.

```python
_ASSET_SETS = [
    ("사건id", "화면에 뜰 이름", "이_폴더_이름", "#탭색", 시드),
]
```

---

## 파일명 규약 — 틀리면 화면이 못 찾습니다

| 종류 | 규약 | 예 |
|---|---|---|
| 구역 배경 | `{사건id}_room_{art키}.webp` | `template_room_scene.webp` |
| 카드 그림 | `{사건id}_card_{카드ID}.webp` | `template_card_A1.webp` |
| 인물별 카드 | `{사건id}_card_{카드ID}_{배역id}.webp` | `template_card_B2_beta.webp` |
| 인물 초상 | `{사건id}_portrait_{배역id}.webp` | `template_portrait_alpha.webp` |
| 오프닝 컷 | `{사건id}_opening_{img키}.webp` | `template_opening_calm.webp` |
| 금고 서류 | `{사건id}_doc_{키}.webp` | `template_doc_will.webp` |
| 카드 뒷면 | `{사건id}_cardback.webp` | `template_cardback.webp` |
| 지도 | `{사건id}_map.svg` | `template_map.svg` |
| 포스터 | `poster_{사건id}.jpg` | `poster_template.jpg` |

`{사건id}` 는 화면에 뜨는 제목이 아니라 **모듈의 `ID`** 입니다.

**구역 배경은 시나리오가 `ROOMS` 를 들고 있어야 화면이 찾아갑니다.** 파일만
넣으면 안 뜹니다 — `art` 키가 파일명의 그 자리입니다.

없는 파일은 자동으로 폴백됩니다. **일부만 넣어도 화면이 안 깨집니다.**
