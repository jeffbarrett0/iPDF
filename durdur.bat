@echo off
cd /d "%~dp0"
docker compose down
echo  Durduruldu. Dosyalariniz silinmedi. / Stopped. Your files are kept.
pause
