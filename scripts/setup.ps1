#Requires -RunAsAdministrator
<#
.SYNOPSIS
    AgentLaunch HQ – Instalador Automático para Windows

.DESCRIPTION
    Instala e configura o WSL2 com Ubuntu, prepara o ambiente do AgentLaunch HQ
    e registra o supervisor de segurança no Agendador de Tarefas do Windows
    (execução 4x ao dia: 00:00, 06:00, 12:00, 18:00).

    ALTERNATIVA SEM WSL: Conecte este repositório diretamente no Conway Cloud
    Sandbox — o script setup.sh funciona nativamente em qualquer container Linux.

.PARAMETER ApenasAgendador
    Pula a instalação/configuração do WSL e apenas recria as tarefas agendadas.

.EXAMPLE
    # Instalação completa (execute como Administrador):
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1

    # Somente recriar as tarefas agendadas:
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -ApenasAgendador
#>

param(
    [switch]$ApenasAgendador
)

$ErrorActionPreference = "Stop"
$NomeTarefa = "AgentLaunchHQ_Supervisor"
$RepoURL    = "https://github.com/eduardocmoro/marketingAG_Automatons.git"

# -----------------------------------------------------------------------
function Escrever-Etapa([string]$Num, [string]$Msg) {
    Write-Host "`n[$Num] $Msg" -ForegroundColor Cyan
}

function Testar-WSLDisponivel {
    return [bool](Get-Command wsl -ErrorAction SilentlyContinue)
}

function Testar-UbuntuInstalado {
    try {
        $distros = (wsl -l -q 2>&1) -join " "
        return $distros -match "Ubuntu"
    } catch { return $false }
}

function Invocar-WSL([string]$Cmd) {
    $resultado = wsl bash -c $Cmd
    if ($LASTEXITCODE -ne 0) {
        throw "Comando WSL falhou (código $LASTEXITCODE): $Cmd"
    }
    return $resultado
}
# -----------------------------------------------------------------------

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Green
Write-Host "  AgentLaunch HQ — Instalador para Windows" -ForegroundColor Green
Write-Host "==============================================================" -ForegroundColor Green

# ==============================================================================
# ETAPA 1: Instalar / verificar WSL2
# ==============================================================================
if (-not $ApenasAgendador) {

    Escrever-Etapa "1/4" "Verificando WSL2..."

    if (-not (Testar-WSLDisponivel)) {
        Write-Host "  WSL2 nao encontrado. Iniciando instalacao..." -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  ATENCAO: O Windows solicitara uma REINICIALIZACAO." -ForegroundColor Red
        Write-Host "  Apos reiniciar, abra este script novamente como Administrador." -ForegroundColor Red
        Write-Host ""

        wsl --install -d Ubuntu
        Write-Host ""
        Write-Host "  Reinicie o Windows e execute este script novamente para continuar." -ForegroundColor Yellow
        Read-Host "  Pressione ENTER para sair"
        exit 0
    }

    Write-Host "  WSL2 disponivel." -ForegroundColor Green
    wsl --set-default-version 2 2>$null | Out-Null

    if (-not (Testar-UbuntuInstalado)) {
        Write-Host "  Ubuntu nao encontrado. Instalando..." -ForegroundColor Yellow
        wsl --install -d Ubuntu --no-launch
        Write-Host "  Ubuntu instalado. Pode ser necessario configurar usuario/senha na" -ForegroundColor Yellow
        Write-Host "  primeira abertura: execute 'wsl' no terminal e siga as instrucoes." -ForegroundColor Yellow
    } else {
        Write-Host "  Ubuntu (WSL) disponivel." -ForegroundColor Green
    }

    # ==============================================================================
    # ETAPA 2: Garantir Node.js + pnpm no WSL (prereqs do build Automaton)
    # ==============================================================================
    Escrever-Etapa "2/4" "Verificando Node.js e pnpm no WSL..."

    # Usamos single-quotes para que o PowerShell NAO expanda $HOME/$NVM_DIR —
    # a expansao e feita pelo bash dentro do WSL.
    $cmdNode = 'source $HOME/.bashrc 2>/dev/null; source $HOME/.profile 2>/dev/null; ' +
               'export NVM_DIR="$HOME/.nvm"; [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; ' +
               'if ! command -v node &>/dev/null; then ' +
               '  echo "Instalando Node.js via nvm..."; ' +
               '  curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash; ' +
               '  export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; ' +
               '  nvm install --lts; ' +
               'fi; ' +
               'if ! command -v pnpm &>/dev/null; then ' +
               '  echo "Instalando pnpm..."; npm install -g pnpm; ' +
               'fi; ' +
               'echo "Node: $(node --version)  pnpm: $(pnpm --version)"'

    Invocar-WSL $cmdNode

    # ==============================================================================
    # ETAPA 3: Clonar repositorio no WSL e executar setup.sh
    # ==============================================================================
    Escrever-Etapa "3/4" "Configurando AgentLaunch HQ no WSL..."

    $cmdSetup = 'export NVM_DIR="$HOME/.nvm"; [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; ' +
                "if [ ! -d ~/marketingAG_Automatons ]; then " +
                "  echo 'Clonando repositorio AgentLaunch HQ...'; " +
                "  git clone '$RepoURL' ~/marketingAG_Automatons; " +
                "else " +
                "  echo 'Repositorio ja existe. Atualizando...'; " +
                "  git -C ~/marketingAG_Automatons pull; " +
                "fi; " +
                'cd ~/marketingAG_Automatons && bash scripts/setup.sh'

    Invocar-WSL $cmdSetup

    Write-Host "  Ambiente configurado com sucesso no WSL." -ForegroundColor Green
}

# ==============================================================================
# ETAPA 4: Registrar supervisor no Agendador de Tarefas do Windows
# ==============================================================================
Escrever-Etapa "4/4" "Registrando tarefas no Agendador de Tarefas do Windows..."

# Remover tarefa anterior (idempotente)
if (Get-ScheduledTask -TaskName $NomeTarefa -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $NomeTarefa -Confirm:$false
    Write-Host "  Tarefa anterior removida." -ForegroundColor Yellow
}

# O supervisor e instalado em ~/.automaton/claude_supervisor.sh pelo setup.sh.
# O wsl.exe chama bash com esse caminho fixo; ~ e expandido pelo bash no WSL.
$Acao = New-ScheduledTaskAction `
    -Execute  "wsl.exe" `
    -Argument 'bash -c "~/.automaton/claude_supervisor.sh >> ~/.automaton/cron.log 2>&1"'

$Gatilhos = @(
    (New-ScheduledTaskTrigger -Daily -At "00:00"),
    (New-ScheduledTaskTrigger -Daily -At "06:00"),
    (New-ScheduledTaskTrigger -Daily -At "12:00"),
    (New-ScheduledTaskTrigger -Daily -At "18:00")
)

$Configuracoes = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit  (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances   IgnoreNew

$Principal = New-ScheduledTaskPrincipal `
    -UserId   $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName    $NomeTarefa `
    -Action      $Acao `
    -Trigger     $Gatilhos `
    -Settings    $Configuracoes `
    -Principal   $Principal `
    -Description "Supervisor de segurança e auditoria do AgentLaunch HQ. Executa 4x ao dia via WSL." `
    | Out-Null

Write-Host "  Tarefa '$NomeTarefa' registrada." -ForegroundColor Green
Write-Host "  Horarios: 00:00 / 06:00 / 12:00 / 18:00 (horario local)." -ForegroundColor Green

# ==============================================================================
# RESUMO FINAL
# ==============================================================================
Write-Host ""
Write-Host "==============================================================" -ForegroundColor Green
Write-Host "  AGENTLAUNCH HQ CONFIGURADO COM SUCESSO!" -ForegroundColor Green
Write-Host "==============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  INICIAR o agente (terminal PowerShell ou CMD):" -ForegroundColor White
Write-Host '    wsl bash -c "cd ~/automaton && node dist/index.js --run"' -ForegroundColor Cyan
Write-Host ""
Write-Host "  APORTAR os `$5,00 USD iniciais (outro terminal):" -ForegroundColor White
Write-Host '    wsl bash -c "cd ~/automaton && node packages/cli/dist/index.js fund 5.00"' -ForegroundColor Cyan
Write-Host ""
Write-Host "  VERIFICAR o log do supervisor:" -ForegroundColor White
Write-Host '    wsl bash -c "cat ~/.automaton/cron.log"' -ForegroundColor Cyan
Write-Host ""
Write-Host "  ALTERNATIVA — Conway Cloud Sandbox (sem WSL):" -ForegroundColor Yellow
Write-Host "    Conecte este repositorio no Conway Cloud Dashboard," -ForegroundColor White
Write-Host "    abra um Sandbox Linux e execute: bash scripts/setup.sh" -ForegroundColor White
Write-Host "==============================================================" -ForegroundColor Green
Write-Host ""
