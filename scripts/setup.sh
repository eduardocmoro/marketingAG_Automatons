#!/bin/bash
# ==============================================================================
# SCRIPT DE IMPLANTAÇÃO AGENTLAUNCH HQ
# Execute este script UMA VEZ na sua máquina para configurar o ambiente.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

echo "=============================================================="
echo " AgentLaunch HQ - Setup de Ambiente"
echo "=============================================================="

# 1. Clonar e construir o runtime Automaton
if [ ! -d "$HOME/automaton" ]; then
    echo "[1/5] Clonando repositório Conway Research Automaton..."
    git clone https://github.com/Conway-Research/automaton.git "$HOME/automaton"
else
    echo "[1/5] Repositório Automaton já existe em $HOME/automaton — ignorando clone."
fi

echo "[2/5] Instalando dependências e realizando build..."
cd "$HOME/automaton"
pnpm install
pnpm build

# 2. Criar estrutura de dados local do agente
echo "[3/5] Criando estrutura de diretórios e arquivos de estado..."
mkdir -p "$HOME/.automaton/intelligence"
touch "$HOME/.automaton/blacklist.db"
touch "$HOME/.automaton/compliance.log"
touch "$HOME/.automaton/learning_database.json"
touch "$HOME/.automaton/social_whitelist.json"
touch "$HOME/.automaton/daily_audit_report.txt"

# 3. Copiar arquivos de configuração deste repositório para o local esperado
echo "[4/5] Instalando configuração e prompt genesis..."
cp "$REPO_DIR/config/AGENTLAUNCH_CONSTITUTION.md" "$HOME/.automaton/AGENTLAUNCH_CONSTITUTION.md"
cp "$REPO_DIR/config/genesis_prompt.txt" "$HOME/.automaton/genesis_prompt.txt"

# 4. Instalar e agendar o supervisor via cron (4x ao dia: 00:00, 06:00, 12:00, 18:00)
echo "[5/5] Agendando supervisor de segurança no cron..."
SUPERVISOR_PATH="$REPO_DIR/scripts/claude_supervisor.sh"
chmod +x "$SUPERVISOR_PATH"
(crontab -l 2>/dev/null | grep -v "claude_supervisor.sh"; \
 echo "0 0,6,12,18 * * * $SUPERVISOR_PATH >> $HOME/.automaton/cron.log 2>&1") | crontab -

echo ""
echo "=============================================================="
echo " SISTEMA AGENTLAUNCH HQ CONFIGURADO COM SUCESSO!"
echo " Supervisor agendado: 4x ao dia (00:00, 06:00, 12:00, 18:00)."
echo ""
echo " Para iniciar o runtime do agente:"
echo "   cd $HOME/automaton && node dist/index.js --run"
echo ""
echo " Para aportar os \$5.00 USD iniciais (em outro terminal):"
echo "   cd $HOME/automaton && node packages/cli/dist/index.js fund 5.00"
echo "=============================================================="
