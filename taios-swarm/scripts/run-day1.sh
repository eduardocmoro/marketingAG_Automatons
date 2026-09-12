#!/bin/bash
# ==============================================================================
# TAIOS-Swarm — Day 1: acumular amostras de custo e gerar o relatório
#
# Uso:
#   bash scripts/run-day1.sh              # 1 amostra
#   bash scripts/run-day1.sh 12 300       # 12 amostras, 300s entre elas (1 hora)
#
# Emenda 6: o arquivo é append-only. Rode ao longo de dias e horários
# diferentes — um retrato único não serve para estimar distribuição.
#
# Env opcionais:
#   SOLANA_RPC_URL   RPC dedicado (Helius/QuickNode/Alchemy). O público
#                    (api.mainnet-beta.solana.com) é rate-limited e costuma
#                    recusar simulateTransaction.
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
echo "RPC      : ${SOLANA_RPC_URL:-https://api.mainnet-beta.solana.com (público)}"
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
