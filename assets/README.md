# 이미지 에셋

서버가 이 폴더를 `/assets/` 로 서빙합니다. 파일을 넣는 즉시 UI가 집어갑니다.

## 인물 사진 (배역 선택 카드)

파일명 규약:

    {시나리오ID}_portrait_{배역ID}.png

없으면 이모지로 자동 폴백되므로, 일부만 넣어도 화면이 깨지지 않습니다.

| 시나리오 | 배역 ID |
|---|---|
| `submarine` | munjaei, kangyunseo, oserin, jinharam, yutaeo, handokyung |
| `subway` | han, ora, mun, yun |
| `graduation` | sim, yu, lee, ose |

예) `submarine_portrait_handokyung.png`

- 비율 **3:4** (권장 768×1024). 카드에서 `object-fit:cover`로 잘립니다.
- 얼굴이 위쪽 60%에 오게 잡으세요. 카드 하단은 이름/직업 그라데이션이 덮습니다.
- base64로 HTML에 넣지 마세요. 파일로 두는 편이 로딩에 유리합니다.

## 스포일러 주의

진범·반전 배역은 **겉모습 그대로** 그려야 합니다. 죄책감·악역 단서를 넣지 마세요.
해당 배역: `submarine/handokyung`, `subway/yun`, `graduation/ose`.

자세한 프롬프트는 `docs/asset-manifest.md` 참고.

## 오프닝 배경 (비주얼노벨)

파일명 규약:

    {시나리오ID}_opening_{컷}.png

| 컷 | 파일 | 내용 |
|---|---|---|
| `calm`  | `submarine_opening_calm.png`  | 사건 전, 정상 조명의 평온한 복도 |
| `bg`    | `submarine_opening_bg.png`    | 붉은 비상등이 켜진 복도 |
| `body`  | `submarine_opening_body.png`  | 선장의 시신 (절제된 연출) |
| `scene` | `submarine_opening_scene.png` | 함교 사건 현장 |

- 가로 **16:9** (권장 1920×1080), 장당 **600KB 이하**.
- 화면 아래 40%에는 내레이션이 올라가므로 그쪽은 어둡게 비워둘 것.
- 없는 컷은 CSS 그라데이션으로 자동 폴백된다 — 일부만 넣어도 된다.
