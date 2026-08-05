@echo off
chcp 65001 >nul
cd /d "%~dp0"
title PIMMmurderboard - 그해 여름, 동창회

if not exist ".venv" (
  echo [최초 1회] 가상환경을 만들고 필요한 부품을 설치합니다...
  python -m venv .venv
  ".venv\Scripts\pip.exe" install -q --upgrade pip
  ".venv\Scripts\pip.exe" install -q -r requirements.txt
)

if not exist ".env" (
  copy .env.example .env >nul
  echo [안내] .env 파일을 만들었습니다. ANTHROPIC_API_KEY 를 넣고 다시 실행하세요.
  echo        (무료 로컬 모델을 쓰려면 .env 에서 LLM_BACKEND=ollama)
  pause
  exit /b
)

echo 서버를 시작합니다. (같은 와이파이면 폰으로도 접속 가능 - 아래 주소 확인)
".venv\Scripts\python.exe" server.py
pause
