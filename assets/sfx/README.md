# UI 효과음

지금은 **전부 Web Audio로 합성**해서 울린다(파일 0개). 여기에 같은 이름의 음원을 올리면
코드를 고치지 않고 그 파일이 대신 울린다. 확장자는 `ogg · m4a · mp3 · wav` 순으로 찾는다.

| 파일명 | 언제 울리나 | 알만툴(RPG Maker MZ) 대응 |
|---|---|---|
| `equip` | 일반 버튼 누름 | `Equip1.ogg` |
| `decision` | 확정 버튼(금색·붉은색) | `Decision1.ogg` |
| `cancel` | 닫기·취소 | `Cancel1.ogg` |
| `book` | 카드 조사·카드 선택 | `Book1.ogg` |
| `cursor` | 하단 독으로 창 열기 | `Cursor1.ogg` |
| `buzzer` | 막힌 동작·잠긴 카드·오류 | `Buzzer1.ogg` |
| `bell` | 성공·좋은 결과 | `Bell1.ogg` |
| `siren` | 파공 발생(침수 퍼즐) | — |
| `pod` | 탈출 포드 개방 | `Gate1.ogg` |
| `reveal` | 진상 공개 | — |
| `turn` | 내 차례가 왔다 | — |
| `flood` | 물이 차오른다 | — |

## 알만툴 SE를 쓸 경우

드라이브 `.../audio/se` 폴더(`1S5eqbyLpABfWactwqSh0W5iL9EKACejs`)에서 위 표의 파일을
받아 **표의 왼쪽 이름으로 바꿔** 이 폴더에 두면 된다. 예: `Equip1.ogg` → `equip.ogg`.

주의 — RPG Maker RTP 음원은 라이선스가 「RPG Maker로 제작한 게임」에 한정된다.
이 보드는 RPG Maker 산출물이 아니므로, 외부에 공개 배포하거나 상업화할 때는
CC0 음원(Kenney, freesound CC0 등)으로 교체해야 한다. 파일명만 같게 두면 교체는 끝난다.
