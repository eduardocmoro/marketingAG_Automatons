#!/bin/bash
# ==============================================================================
# AgentLaunch HQ — Executar Teste de Contato Multi-Canal
#
# Uso:
#   bash scripts/run-contact-test.sh [agentId]
#
# Com email via SendGrid:
#   export SENDGRID_API_KEY="SG.seu_token_aqui"
#   export SENDGRID_FROM="seuemail@dominio.com"
#   bash scripts/run-contact-test.sh
#
# Agente padrão: 1 (ClawNews, primeiro agente no ERC-8004)
# ==============================================================================

set -e

AUTOMATON_RUNTIME="$HOME/automaton"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_ID="${1:-1}"

echo ""
echo "AgentLaunch HQ — Teste de Contato Multi-Canal"
echo "Agente alvo: ERC-8004 #${AGENT_ID}"
echo ""

# Verificar runtime
if [ ! -d "$AUTOMATON_RUNTIME/node_modules/viem" ]; then
    echo "ERRO: ~/automaton/node_modules/viem não encontrado."
    echo "Execute setup.sh primeiro e depois configure-byok.sh"
    exit 1
fi

# Verificar config
if [ ! -f "$HOME/.automaton/automaton.json" ]; then
    echo "ERRO: ~/.automaton/automaton.json não encontrado."
    echo "Execute configure-byok.sh primeiro."
    exit 1
fi

# Carregar NVM
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

# Copiar script para ~/automaton (para resolver módulos ESM corretamente)
TEMP_SCRIPT="$AUTOMATON_RUNTIME/_agentlaunch_contact_test.mjs"
cp "$SCRIPT_DIR/test-contact.mjs" "$TEMP_SCRIPT"

# Executar
cd "$AUTOMATON_RUNTIME"
TARGET_AGENT_ID="$AGENT_ID" node "$TEMP_SCRIPT"
EXIT_CODE=$?

# Limpar
rm -f "$TEMP_SCRIPT"

exit $EXIT_CODE
