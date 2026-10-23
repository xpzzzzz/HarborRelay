$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

docker info --format '{{.ServerVersion}}'
if ($LASTEXITCODE -ne 0) { throw 'Please start Docker Desktop first.' }
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.docker.example' -Destination '.env'
    Write-Host 'Created .env from the Docker example.'
}

$taskSettings = @{}
foreach ($taskLine in Get-Content -LiteralPath '.env' -Encoding UTF8) {
    if ($taskLine -match '^\s*([A-Z_][A-Z0-9_]*)\s*=(.*)$') {
        $taskSettings[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
    }
}
$taskUsesOllamaChat = $taskSettings['AI_PROVIDER'] -eq 'ollama'
$taskUsesOllamaEmbeddings = $taskSettings['EMBEDDING_PROVIDER'] -eq 'ollama'
if ($taskUsesOllamaChat -or $taskUsesOllamaEmbeddings) {
    $taskModels = (Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 10).models.name
    $taskChatModel = $taskSettings['OLLAMA_MODEL']
    if ($taskUsesOllamaChat -and $taskModels -notcontains $taskChatModel) {
        throw "The Ollama chat model '$taskChatModel' is missing. See README.md."
    }
    $taskEmbeddingModel = $taskSettings['OLLAMA_EMBEDDING_MODEL']
    if ($taskUsesOllamaEmbeddings -and $taskModels -notcontains $taskEmbeddingModel) {
        ollama pull $taskEmbeddingModel
        if ($LASTEXITCODE -ne 0) { throw 'Unable to download the embedding model.' }
    }
}

docker compose up -d --build --wait --wait-timeout 240
if ($LASTEXITCODE -ne 0) { throw 'Startup failed. Run docker compose logs --tail 100.' }
Write-Host 'HarborRelay is ready: http://127.0.0.1:8080'
