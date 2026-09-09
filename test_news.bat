@echo off
cd /d "%~dp0dashboard"
python -c "from news_service import run_once; d=run_once(); print('OK | bias=',d.get('bias'),'conf=',d.get('confidence'),'risk=',d.get('risk'),'articles=',d.get('article_count'),'sources=',d.get('sources_ok'), '/', d.get('source_count'))"
pause
