#!/usr/bin/env bash
# PIMMmurderboard 실행 (mac / linux)
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "[최초 1회] 가상환경을 만들고 필요한 부품을 설치합니다…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "[안내] .env 파일을 만들었습니다. ANTHROPIC_API_KEY 를 넣고 다시 실행하세요."
  echo "       (무료 로컬 모델을 쓰려면 .env 에서 LLM_BACKEND=ollama)"
  exit 0
fi

echo "서버를 시작합니다. (같은 와이파이면 폰으로도 접속 가능 — 아래 주소 확인)"
exec ./.venv/bin/python server.py
