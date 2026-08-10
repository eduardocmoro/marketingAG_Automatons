#!/bin/bash
# ==============================================================================
# SCRIPT DE IMPLANTAÇÃO AGENTLAUNCH HQ
# Execute este script UMA VEZ na sua máquina para configurar o ambiente.
# Compatível com: Linux nativo, WSL2 (Ubuntu) e Conway Cloud Sandbox.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

echo "=============================================================="
echo " AgentLaunch HQ - Setup de Ambiente"
echo "=============================================================="

# ──────────────────────────────────────────────────────────────────
# PRÉ-REQUISITO: Node.js (via nvm) e pnpm
# ──────────────────────────────────────────────────────────────────
echo "[0/6] Verificando Node.js e pnpm..."

export NVM_DIR="$HOME/.nvm"

if ! command -v node &>/dev/null; then
    echo "  Node.js não encontrado. Instalando via nvm..."
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
    nvm install --lts
else
    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
    echo "  Node.js encontrado: $(node --version)"
fi

if ! command -v pnpm &>/dev/null; then
    echo "  pnpm não encontrado. Instalando..."
    npm install -g pnpm
else
    echo "  pnpm encontrado: $(pnpm --version)"
fi

# ──────────────────────────────────────────────────────────────────
# 1. Clonar e construir o runtime Automaton
# ──────────────────────────────────────────────────────────────────
if [ ! -d "$HOME/automaton" ]; then
    echo "[1/6] Clonando repositório Conway Research Automaton..."
    git clone https://github.com/Conway-Research/automaton.git "$HOME/automaton"
else
    echo "[1/6] Repositório Automaton já existe em $HOME/automaton — ignorando clone."
fi

echo "[2/6] Instalando dependências e realizando build..."
cd "$HOME/automaton"
pnpm install
pnpm build

# ──────────────────────────────────────────────────────────────────
# 2. Criar estrutura de dados local do agente
# ──────────────────────────────────────────────────────────────────
echo "[3/6] Criando estrutura de diretórios e arquivos de estado..."
mkdir -p "$HOME/.automaton/intelligence"
touch "$HOME/.automaton/blacklist.db"
touch "$HOME/.automaton/compliance.log"
touch "$HOME/.automaton/learning_database.json"
touch "$HOME/.automaton/social_whitelist.json"
touch "$HOME/.automaton/daily_audit_report.txt"

# ──────────────────────────────────────────────────────────────────
# 3. Copiar arquivos de configuração para o local esperado pelo runtime
# ──────────────────────────────────────────────────────────────────
echo "[4/6] Instalando configuração e prompt genesis..."
cp "$REPO_DIR/config/AGENTLAUNCH_CONSTITUTION.md" "$HOME/.automaton/AGENTLAUNCH_CONSTITUTION.md"
cp "$REPO_DIR/config/genesis_prompt.txt"           "$HOME/.automaton/genesis_prompt.txt"

# ──────────────────────────────────────────────────────────────────
# 4. Instalar o supervisor em caminho fixo (~/.automaton/claude_supervisor.sh)
#    O Agendador de Tarefas do Windows (via wsl.exe) e o cron Linux
#    usam sempre este caminho fixo.
# ──────────────────────────────────────────────────────────────────
echo "[5/6] Instalando supervisor em ~/.automaton/..."
cp "$REPO_DIR/scripts/claude_supervisor.sh" "$HOME/.automaton/claude_supervisor.sh"
chmod +x "$HOME/.automaton/claude_supervisor.sh"

# ──────────────────────────────────────────────────────────────────
# 5. Agendar supervisor via cron (Linux nativo / WSL com cron ativo)
#    No Windows, o Agendador de Tarefas é configurado pelo setup.ps1.
# ──────────────────────────────────────────────────────────────────
echo "[6/6] Agendando supervisor de segurança no cron (Linux/WSL)..."

# Verificar se estamos no WSL
if grep -qi microsoft /proc/version 2>/dev/null; then
    echo "  Ambiente WSL detectado — agendamento via Agendador de Tarefas do Windows."
    echo "  O setup.ps1 já cuida disso. Cron do WSL não configurado automaticamente."
else
    SUPERVISOR_FIXO="$HOME/.automaton/claude_supervisor.sh"
    (crontab -l 2>/dev/null | grep -v "claude_supervisor.sh"; \
     echo "0 0,6,12,18 * * * $SUPERVISOR_FIXO >> $HOME/.automaton/cron.log 2>&1") | crontab -
    echo "  Cron configurado: 00:00, 06:00, 12:00, 18:00."
fi

echo ""
echo "=============================================================="
echo " SISTEMA AGENTLAUNCH HQ CONFIGURADO COM SUCESSO!"
echo ""
echo " Para iniciar o runtime do agente:"
echo "   cd $HOME/automaton && node dist/index.js --run"
echo ""
echo " Para aportar os \$5,00 USD iniciais (outro terminal):"
echo "   cd $HOME/automaton && node packages/cli/dist/index.js fund 5.00"
echo ""
echo " Supervisor instalado em: ~/.automaton/claude_supervisor.sh"
echo " Log de auditoria:        ~/.automaton/cron.log"
echo "=============================================================="
