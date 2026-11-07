# Roda a busca no seu PC (Windows) e publica no GitHub.
# Útil se os sites bloquearem os servidores do GitHub Actions.
# Uso: abra o PowerShell na pasta do projeto e rode  .\run_local.ps1
# Para agendar: Agendador de Tarefas > Criar Tarefa Básica > Diariamente > Programa: powershell.exe
#   Argumentos: -ExecutionPolicy Bypass -File "C:\Users\Anderson\source\apto-hunter\run_local.ps1"

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    python -m venv .venv
    .\.venv\Scripts\pip install -r requirements.txt
    .\.venv\Scripts\python -m playwright install chromium
}

# Preencha com os mesmos valores dos Secrets do GitHub (ou defina como variáveis de ambiente do Windows)
if (-not $env:TELEGRAM_TOKEN)   { $env:TELEGRAM_TOKEN   = "" }
if (-not $env:TELEGRAM_CHAT_ID) { $env:TELEGRAM_CHAT_ID = "" }

git pull --rebase
.\.venv\Scripts\python -m apto.main
git add data/listings.json docs/data.json
git commit -m "Atualiza anúncios (local) $(Get-Date -Format yyyy-MM-dd)"
git push
