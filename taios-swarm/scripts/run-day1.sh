#!/bin/bash
# ==============================================================================
# TAIOS-Swarm — Day 1: acumular amostras de custo e gerar o relatório
#
# Uso:
#   bash scripts/run-day1.sh              # 1 amostra
#   bash scripts/run-day1.sh 8 300        # 1 bloco: 8 amostras a cada 5 min
#
# IMPORTANTE — um bloco NÃO é suficiente.
# 8 amostras seguidas cobrem ~40 min: um único regime de congestionamento.
# São necessários no mínimo 3 blocos em horários distintos ao longo de 2 dias,
# acumulando no MESMO jsonl (o arquivo é append-only). O relatório avisa
# enquanto a cobertura for insuficiente e não deve ser usado para decidir
# nada antes disso.
#
# Sugestão de agenda (horários UTC bem separados):
#   dia 1, manhã   : bash scripts/run-day1.sh 8 300
#   dia 1, noite   : bash scripts/run-day1.sh 8 300
#   dia 2, tarde   : bash scripts/run-day1.sh 8 300
#
# Env opcionais:
#   SOLANA_RPC_URL   RPC dedicado (Helius/QuickNode/Alchemy). O público
#                    (api.mainnet-beta.solana.com) é rate-limited e costuma
#                    recusar simulateTransaction.
#                    A URL nunca é impressa nem gravada por inteiro.
#   JUP_MIN_GAP_MS   espaçamento entre chamadas Jupiter (padrão 1200).
#                    Suba para 2000+ se aparecer 429.
#   MEASURE_PUBKEY   conta pública usada só para simular e obter unitsConsumed
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"

SAMPLES="${1:-1}"
INTERVAL="${2:-0}"

export NVM_DIR="$HOME/.nvm"
# shellcheck disable=SC1091
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

if ! command -v node >/dev/null 2>&1; then
    echo "ERRO: node não encontrado."
    exit 1
fi

echo "TAIOS-Swarm — Day 1"
echo "Amostras : $SAMPLES"
echo "Intervalo: ${INTERVAL}s"
# Nunca imprimir a URL completa: RPC dedicado carrega a API key nela.
if [ -n "${SOLANA_RPC_URL:-}" ]; then
    RPC_HOST="$(printf '%s' "$SOLANA_RPC_URL" | sed -E 's#^(https?://[^/?]+).*#\1#')"
    RPC_TAIL="$(printf '%s' "$SOLANA_RPC_URL" | tail -c 5)"
    echo "RPC      : ${RPC_HOST}/…${RPC_TAIL}"
else
    echo "RPC      : https://api.mainnet-beta.solana.com (público)"
fi
echo ""

if [ -z "${SOLANA_RPC_URL:-}" ]; then
    echo "AVISO: usando RPC público. simulateTransaction costuma ser recusado,"
    echo "       e nesse caso cu_consumed cai para a estimativa do Jupiter"
    echo "       (registrado como cuConsumedSource=jupiter_estimate)."
    echo "       Para medir de verdade, use um RPC dedicado:"
    echo "         export SOLANA_RPC_URL='https://mainnet.helius-rpc.com/?api-key=SUA_CHAVE'"
    echo ""
fi

cd "$ROOT"

for i in $(seq 1 "$SAMPLES"); do
    echo "--- amostra $i/$SAMPLES ---"
    node scripts/measure-day1.mjs || echo "  (amostra $i falhou; erros ficam registrados no jsonl)"
    if [ "$i" -lt "$SAMPLES" ] && [ "$INTERVAL" -gt 0 ]; then
        sleep "$INTERVAL"
    fi
done

echo ""
echo "=== RELATÓRIO ==="
python3 analysis/day1_report.py
