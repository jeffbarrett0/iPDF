@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  iPDF - baslatiliyor / starting...
echo.
docker --version >nul 2>&1
if errorlevel 1 (
  echo  Docker Desktop kurulu degil / is not installed.
  echo  Indirin / download: https://www.docker.com/products/docker-desktop/
  pause
  exit /b 1
)
docker info >nul 2>&1
if errorlevel 1 (
  echo  Docker Desktop calismiyor. Once Docker Desktop'i acin, "Engine running" yazisini bekleyin, sonra tekrar deneyin.
  echo  Docker Desktop is not running. Open it, wait until it says "Engine running", then try again.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup-env.ps1"
if errorlevel 1 ( echo  .env olusturulamadi / could not create .env & pause & exit /b 1 )
if not exist backups mkdir backups
echo  Ilk calistirma 10-20 dakika surebilir (indirme ve derleme). / First run can take 10-20 minutes.
docker compose up -d --build --wait
if errorlevel 1 (
  echo.
  echo  Baslatilamadi. Hata ayrintisi icin: docker compose logs
  echo  Failed to start. For details run: docker compose logs
  pause
  exit /b 1
)
start "" http://localhost:3000
echo.
echo  Hazir! Tarayici acildi: http://localhost:3000
echo  Durdurmak icin durdur.bat dosyasina cift tiklayin.
echo.
pause
