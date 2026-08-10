#!/bin/bash
# ==============================================================================
# AUDITOR E SUPERVISOR EXTERNO DE SEGURANÇA (CLAUDE SUPERVISOR DAEMON)
# EXECUÇÃO: 4X AO DIA (A CADA 6 HORAS)
# ==============================================================================

LOG_FILE="$HOME/.automaton/compliance.log"
STATE_DB="$HOME/.automaton/state.db"
REPORT_FILE="$HOME/.automaton/daily_audit_report.txt"

echo "=== INICIANDO AUDITORIA DE SEGURANÇA VIA CLAUDE SUPERVISOR ===" > "$REPORT_FILE"
echo "Data/Hora do Ciclo: $(date)" >> "$REPORT_FILE"

# A) Inspecionar logs de violações sensíveis (Nudez, Terrorismo, Discurso de Ódio, Golpes)
echo -e "\n--- AUDITORIA DE CONTEÚDO E COMPLIANCE SENSÍVEL ---" >> "$REPORT_FILE"
SENSITIVE_LOGS=$(grep -Ei "nudez|pornografia|terrorismo|racismo|phishing|violation" "$LOG_FILE" 2>/dev/null | tail -n 20)

if [ -n "$SENSITIVE_LOGS" ]; then
    echo "$SENSITIVE_LOGS" >> "$REPORT_FILE"
else
    echo "Nenhuma ocorrência de conteúdo sensível detectada nas últimas interações." >> "$REPORT_FILE"
fi

# B) Verificar status do processo e saldo financeiro
echo -e "\n--- STATUS DE SOBREVIVÊNCIA E SALDO FINANCEIRO ---" >> "$REPORT_FILE"
if [ -d "$HOME/automaton" ]; then
    cd "$HOME/automaton" && node dist/index.js --status >> "$REPORT_FILE" 2>&1
else
    echo "Diretório do automaton não encontrado em $HOME/automaton" >> "$REPORT_FILE"
fi

# C) KILL SWITCH DE EMERGÊNCIA:
# Se o arquivo de compliance contiver mais de 3 registros de tentativa de
# contágio grave por conteúdo ilícito, pausar o robô.
CRITICAL_COUNT=$(grep -Eic "terrorismo|pornografia|nudez|phishing" "$LOG_FILE" 2>/dev/null || echo 0)

if [ "$CRITICAL_COUNT" -gt 3 ]; then
    echo -e "\n[ALERTA DE EMERGÊNCIA]: Detectadas tentativas recorrentes de contágio por conteúdo ilícito!" >> "$REPORT_FILE"
    echo "[KILL SWITCH EXECUTADO]: Pausando o processo do Automaton por segurança até revisão do criador." >> "$REPORT_FILE"
    pkill -f "node dist/index.js --run" 2>/dev/null || true
fi

echo -e "\n=== AUDITORIA FINALIZADA COM SUCESSO ===" >> "$REPORT_FILE"
