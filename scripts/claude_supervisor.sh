#!/bin/bash
# ==============================================================================
# AUDITOR E SUPERVISOR EXTERNO DE SEGURANÇA (CLAUDE SUPERVISOR DAEMON)
# EXECUÇÃO: 4X AO DIA (00:00 / 06:00 / 12:00 / 18:00)
# Acionado por: cron Linux, cron WSL ou Agendador de Tarefas do Windows.
# ==============================================================================

# Carregar o ambiente do usuário para ter acesso a node/pnpm mesmo quando
# disparado pelo cron ou pelo Agendador de Tarefas (que não lê .bashrc).
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
[ -s "$HOME/.profile"  ] && . "$HOME/.profile" 2>/dev/null || true

# Carregar chaves BYOK (Anthropic/OpenAI) salvas pelo configure-byok.sh
if [ -f "$HOME/.automaton/.env" ]; then
    # shellcheck disable=SC1090
    set -a
    . "$HOME/.automaton/.env" 2>/dev/null || true
    set +a
fi

LOG_FILE="$HOME/.automaton/compliance.log"
REPORT_FILE="$HOME/.automaton/daily_audit_report.txt"

echo "=== INICIANDO AUDITORIA DE SEGURANÇA VIA CLAUDE SUPERVISOR ===" > "$REPORT_FILE"
echo "Data/Hora do Ciclo: $(date)" >> "$REPORT_FILE"

# A) Inspecionar logs de violações sensíveis
echo -e "\n--- AUDITORIA DE CONTEÚDO E COMPLIANCE SENSÍVEL ---" >> "$REPORT_FILE"
SENSITIVE_LOGS=$(grep -Ei "nudez|pornografia|terrorismo|racismo|phishing|violation" \
    "$LOG_FILE" 2>/dev/null | tail -n 20)

if [ -n "$SENSITIVE_LOGS" ]; then
    echo "$SENSITIVE_LOGS" >> "$REPORT_FILE"
else
    echo "Nenhuma ocorrência de conteúdo sensível detectada nas últimas interações." >> "$REPORT_FILE"
fi

# B) Verificar status do processo e saldo financeiro
echo -e "\n--- STATUS DE SOBREVIVÊNCIA E SALDO FINANCEIRO ---" >> "$REPORT_FILE"
if [ -d "$HOME/automaton" ]; then
    (cd "$HOME/automaton" && node dist/index.js --status >> "$REPORT_FILE" 2>&1) || \
        echo "Aviso: node dist/index.js --status retornou erro." >> "$REPORT_FILE"
else
    echo "Diretório do automaton não encontrado em $HOME/automaton" >> "$REPORT_FILE"
fi

# C) KILL SWITCH DE EMERGÊNCIA:
# Mais de 3 registros críticos no log → pausar o processo do agente.
CRITICAL_COUNT=$(grep -Eic "terrorismo|pornografia|nudez|phishing" "$LOG_FILE" 2>/dev/null || echo 0)

if [ "$CRITICAL_COUNT" -gt 3 ]; then
    echo -e "\n[ALERTA DE EMERGÊNCIA]: Detectadas tentativas recorrentes de contágio por conteúdo ilícito!" >> "$REPORT_FILE"
    echo "[KILL SWITCH EXECUTADO]: Pausando o processo do Automaton até revisão do criador." >> "$REPORT_FILE"
    pkill -f "node dist/index.js --run" 2>/dev/null || true
fi

echo -e "\n=== AUDITORIA FINALIZADA ===" >> "$REPORT_FILE"
