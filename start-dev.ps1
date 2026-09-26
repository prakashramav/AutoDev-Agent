#!/usr/bin/env pwsh
# start-dev.ps1 — Start both backend (FastAPI) and frontend (Next.js) for local development
# Usage: ./start-dev.ps1

Write-Host ""
Write-Host "  ╔══════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║       AutoDev-Agent — Dev Server         ║" -ForegroundColor Cyan
Write-Host "  ╚══════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Backend  → http://localhost:8000" -ForegroundColor Green
Write-Host "  Frontend → http://localhost:3000" -ForegroundColor Green
Write-Host "  API Docs → http://localhost:8000/docs" -ForegroundColor Yellow
Write-Host ""

# ── Backend ──────────────────────────────────────────────────────────────────
Write-Host "[1/2] Starting FastAPI backend (SQLite mode)..." -ForegroundColor Magenta

$backendJob = Start-Job -ScriptBlock {
    Set-Location "$using:PWD\backend"
    python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload 2>&1
} -Name "AutoDev-Backend"

# Wait for backend to boot
Start-Sleep -Seconds 3

# ── Frontend ─────────────────────────────────────────────────────────────────
Write-Host "[2/2] Starting Next.js frontend..." -ForegroundColor Magenta

$frontendJob = Start-Job -ScriptBlock {
    Set-Location "$using:PWD\frontend"
    npm run dev 2>&1
} -Name "AutoDev-Frontend"

Write-Host ""
Write-Host "Both servers starting... (Ctrl+C to stop)" -ForegroundColor Gray
Write-Host ""

# Stream output from both jobs
try {
    while ($true) {
        $backendOutput = Receive-Job $backendJob -ErrorAction SilentlyContinue
        if ($backendOutput) {
            $backendOutput | ForEach-Object { Write-Host "[API] $_" -ForegroundColor DarkCyan }
        }
        $frontendOutput = Receive-Job $frontendJob -ErrorAction SilentlyContinue
        if ($frontendOutput) {
            $frontendOutput | ForEach-Object { Write-Host "[UI]  $_" -ForegroundColor DarkGreen }
        }
        Start-Sleep -Milliseconds 500
    }
} finally {
    Write-Host "Stopping servers..." -ForegroundColor Yellow
    Stop-Job $backendJob, $frontendJob -ErrorAction SilentlyContinue
    Remove-Job $backendJob, $frontendJob -ErrorAction SilentlyContinue
}
