# Creates .env from .env.example with fresh random secrets (Windows PowerShell 5+).
Set-Location (Split-Path -Parent $PSScriptRoot)
if (Test-Path .env) { exit 0 }
$text = Get-Content .env.example -Raw
foreach ($key in 'PG_PASSWORD', 'SECRET_KEY') {
  $bytes = New-Object byte[] 24
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $value = ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
  $text = $text -replace "(?m)^$key=change-me\r?$", "$key=$value"
}
[IO.File]::WriteAllText((Join-Path (Get-Location) '.env'), $text)
