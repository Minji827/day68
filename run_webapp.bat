@echo off
docker compose up -d
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\webapp.py"
