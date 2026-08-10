#!/bin/bash
# ==============================================================================
# CONFIGURAÇÃO BYOK (Bring Your Own Key) — SEM CONWAY CLOUD
#
# Configura o Automaton usando chave de API própria (Anthropic ou OpenAI),
# sem depender do Conway Cloud (desativado para novos usuários desde 2025).
#
# Uso:
#   export ANTHROPIC_API_KEY="sk-ant-..."
#   bash scripts/configure-byok.sh
#
#   — ou —
#
#   export OPENAI_API_KEY="sk-..."
#   bash scripts/configure-byok.sh
#
# O script:
#   1. Detecta qual chave está disponível
#   2. Para Anthropic: aplica patch no fonte do Automaton e reconstrói
#   3. Gera a carteira Ethereum (via node dist/index.js --init)
#   4. Escreve ~/.automaton/automaton.json com todos os campos necessários
#   5. Escreve ~/.automaton/.env com as chaves (usado pelo supervisor)
# ==============================================================================

set -e

AUTOMATON_DIR="$HOME/.automaton"
AUTOMATON_RUNTIME="$HOME/automaton"
GENESIS_FILE="$AUTOMATON_DIR/genesis_prompt.txt"

echo "=============================================================="
echo " AgentLaunch HQ — Configuracao BYOK (sem Conway Cloud)"
echo "=============================================================="
echo ""

# ──────────────────────────────────────────────────────────────────
# [0] Detectar provedor BYOK
# ──────────────────────────────────────────────────────────────────
if [ -n "$ANTHROPIC_API_KEY" ]; then
    BYOK_PROVIDER="anthropic"
    BYOK_KEY="$ANTHROPIC_API_KEY"
    INFERENCE_MODEL="${INFERENCE_MODEL:-claude-sonnet-5}"
    LOW_COMPUTE_MODEL="claude-haiku-4-5-20251001"
    echo "  Provedor detectado: Anthropic"
    echo "  Modelo principal  : $INFERENCE_MODEL"
elif [ -n "$OPENAI_API_KEY" ]; then
    BYOK_PROVIDER="openai"
    BYOK_KEY="$OPENAI_API_KEY"
    INFERENCE_MODEL="${INFERENCE_MODEL:-gpt-4.1}"
    LOW_COMPUTE_MODEL="gpt-4.1-mini"
    echo "  Provedor detectado: OpenAI"
    echo "  Modelo principal  : $INFERENCE_MODEL"
else
    echo "ERRO: Nenhuma chave de API detectada."
    echo ""
    echo "  Exporte uma das seguintes variaveis antes de executar:"
    echo "    export ANTHROPIC_API_KEY='sk-ant-...'"
    echo "    export OPENAI_API_KEY='sk-...'"
    echo ""
    echo "  IMPORTANTE: nunca commite sua chave real no repositorio."
    exit 1
fi

# ──────────────────────────────────────────────────────────────────
# Verificar runtime
# ──────────────────────────────────────────────────────────────────
if [ ! -f "$AUTOMATON_RUNTIME/dist/index.js" ]; then
    echo ""
    echo "ERRO: Runtime Automaton nao encontrado em $AUTOMATON_RUNTIME/dist/index.js"
    echo "  Execute setup.sh primeiro para clonar e compilar o runtime:"
    echo "    bash scripts/setup.sh"
    exit 1
fi

mkdir -p "$AUTOMATON_DIR"
chmod 700 "$AUTOMATON_DIR"

# ──────────────────────────────────────────────────────────────────
# [1] Patch Anthropic: adiciona modelos Claude ao registro e ao
#     routing matrix do Automaton, depois reconstrói.
#     Pulo para OpenAI (modelos GPT já estão no baseline por padrão).
# ──────────────────────────────────────────────────────────────────
if [ "$BYOK_PROVIDER" = "anthropic" ]; then
    TYPES_FILE="$AUTOMATON_RUNTIME/src/inference/types.ts"

    if grep -q "claude-sonnet-5" "$TYPES_FILE" 2>/dev/null; then
        echo "[1/4] Patch Anthropic ja aplicado — pulando rebuild."
    else
        echo "[1/4] Aplicando patch Anthropic ao runtime..."

        # Carregar NVM para o pnpm
        export NVM_DIR="$HOME/.nvm"
        [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

        # Verificar versao do Node — better-sqlite3 v11 trava em Node 22+
        NODE_MAJOR=$(node --version 2>/dev/null | sed 's/v\([0-9]*\).*/\1/')
        if [ -n "$NODE_MAJOR" ] && [ "$NODE_MAJOR" -ge 22 ]; then
            echo ""
            echo "  AVISO: Node.js v$(node --version) detectado."
            echo "  better-sqlite3 v11 e incompativel com Node 22+."
            echo "  Recomendado: nvm install 20 && nvm use 20 && nvm alias default 20"
            echo ""
            echo "  Continuando assim mesmo — o build pode falhar."
            echo ""
        fi

        # Garantir pnpm disponivel
        if ! command -v pnpm &>/dev/null; then
            echo "  pnpm nao encontrado — instalando via npm..."
            npm install -g pnpm
        fi

        # Usar Python para modificar types.ts de forma segura
        python3 - << 'PYEOF'
import re, sys, os

filepath = os.path.expanduser("~/automaton/src/inference/types.ts")
with open(filepath) as f:
    content = f.read()

# --- 1. Adicionar modelos Claude ao STATIC_MODEL_BASELINE ---
# Haiku com tierMinimum "dead" para funcionar mesmo sem creditos Conway
claude_entries = """  {
    modelId: "claude-sonnet-5",
    provider: "anthropic",
    displayName: "Claude Sonnet 5",
    tierMinimum: "normal",
    costPer1kInput: 30,
    costPer1kOutput: 150,
    maxTokens: 16384,
    contextWindow: 200000,
    supportsTools: true,
    supportsVision: true,
    parameterStyle: "max_tokens",
    enabled: true,
  },
  {
    modelId: "claude-haiku-4-5-20251001",
    provider: "anthropic",
    displayName: "Claude Haiku 4.5",
    tierMinimum: "dead",
    costPer1kInput: 8,
    costPer1kOutput: 40,
    maxTokens: 8192,
    contextWindow: 200000,
    supportsTools: true,
    supportsVision: true,
    parameterStyle: "max_tokens",
    enabled: true,
  },
"""

# Insere antes do ];  que fecha STATIC_MODEL_BASELINE
marker = "export const STATIC_MODEL_BASELINE"
idx = content.find(marker)
if idx == -1:
    print("ERRO: STATIC_MODEL_BASELINE nao encontrado", file=sys.stderr)
    sys.exit(1)

close_bracket = content.find("\n];", idx)
if close_bracket == -1:
    print("ERRO: Fechamento do array nao encontrado", file=sys.stderr)
    sys.exit(1)

content = content[:close_bracket] + "\n" + claude_entries + content[close_bracket:]

# --- 2. Atualizar DEFAULT_ROUTING_MATRIX para preferir Claude ---
# Substituir candidates das tiers high/normal/low_compute/critical
replacements = [
    # high tier
    ('candidates: ["gpt-5.2", "gpt-5.3"], maxTokens: 8192, ceilingCents: -1 },\n    heartbeat_triage',
     'candidates: ["claude-sonnet-5", "gpt-5.2", "gpt-5.3"], maxTokens: 8192, ceilingCents: -1 },\n    heartbeat_triage'),
    ('candidates: ["gpt-5.2", "gpt-5.3"], maxTokens: 4096, ceilingCents: 20',
     'candidates: ["claude-sonnet-5", "gpt-5.2", "gpt-5.3"], maxTokens: 4096, ceilingCents: 20'),
    ('candidates: ["gpt-5.2", "gpt-5-mini"], maxTokens: 4096, ceilingCents: 15',
     'candidates: ["claude-haiku-4-5-20251001", "gpt-5.2", "gpt-5-mini"], maxTokens: 4096, ceilingCents: 15'),
    ('candidates: ["gpt-5.2", "gpt-5.3"], maxTokens: 8192, ceilingCents: -1 },\n  },\n  normal',
     'candidates: ["claude-sonnet-5", "gpt-5.2", "gpt-5.3"], maxTokens: 8192, ceilingCents: -1 },\n  },\n  normal'),
    # normal tier
    ('candidates: ["gpt-5.2", "gpt-5-mini"], maxTokens: 4096, ceilingCents: -1 },\n    heartbeat_triage',
     'candidates: ["claude-sonnet-5", "gpt-5.2", "gpt-5-mini"], maxTokens: 4096, ceilingCents: -1 },\n    heartbeat_triage'),
    ('candidates: ["gpt-5.2", "gpt-5-mini"], maxTokens: 4096, ceilingCents: 10 },\n    summarization',
     'candidates: ["claude-sonnet-5", "gpt-5.2", "gpt-5-mini"], maxTokens: 4096, ceilingCents: 10 },\n    summarization'),
    ('candidates: ["gpt-5.2", "gpt-5-mini"], maxTokens: 4096, ceilingCents: 10 },\n    planning',
     'candidates: ["claude-haiku-4-5-20251001", "gpt-5.2", "gpt-5-mini"], maxTokens: 4096, ceilingCents: 10 },\n    planning'),
    ('candidates: ["gpt-5.2", "gpt-5-mini"], maxTokens: 4096, ceilingCents: -1 },\n  },\n  low_compute',
     'candidates: ["claude-sonnet-5", "gpt-5.2", "gpt-5-mini"], maxTokens: 4096, ceilingCents: -1 },\n  },\n  low_compute'),
    # low_compute tier
    ('candidates: ["gpt-5-mini"], maxTokens: 4096, ceilingCents: 10 },\n    heartbeat_triage',
     'candidates: ["claude-haiku-4-5-20251001", "gpt-5-mini"], maxTokens: 4096, ceilingCents: 10 },\n    heartbeat_triage'),
]

for old, new in replacements:
    content = content.replace(old, new)

# --- 3. Patch tier "dead": adicionar haiku como candidato ---
# Sem isso, o agente fica em dead-tier sem tokens por vez (0 tokens/turn)
# quando o saldo Conway e <= 0 (ou quando a API retorna 401).
import re as _re

def patch_dead_tier(c):
    # Localizar bloco "dead:" e substituir agent_turn candidates
    dead_match = _re.search(r'dead:\s*\{[^}]*agent_turn:\s*\{[^}]*candidates:\s*\[\]', c)
    if dead_match:
        c = c.replace(
            'candidates: [], maxTokens: 0, ceilingCents: 0',
            'candidates: ["claude-haiku-4-5-20251001"], maxTokens: 2048, ceilingCents: -1',
            1  # apenas primeira ocorrencia (dentro do bloco dead)
        )
    return c

content = patch_dead_tier(content)

# --- 4. Atualizar DEFAULT_MODEL_STRATEGY_CONFIG para Claude ---
content = content.replace(
    'inferenceModel: "gpt-5.2",',
    'inferenceModel: "claude-sonnet-5",',
)
content = content.replace(
    'lowComputeModel: "gpt-5-mini",\n  criticalModel: "gpt-5-mini",',
    'lowComputeModel: "claude-haiku-4-5-20251001",\n  criticalModel: "claude-haiku-4-5-20251001",',
)

if "claude-sonnet-5" not in content:
    print("ERRO: Patch nao foi aplicado corretamente.", file=sys.stderr)
    sys.exit(1)

with open(filepath, "w") as f:
    f.write(content)

print("  Patch aplicado: modelos Claude adicionados ao runtime (incluindo tier dead).")
PYEOF

        echo "  Reconstruindo runtime Automaton com suporte Anthropic..."
        cd "$AUTOMATON_RUNTIME"
        pnpm build
        pnpm rebuild better-sqlite3
        echo "  Build concluido."
    fi
else
    echo "[1/4] OpenAI: sem necessidade de patch — modelos GPT ja estao no baseline."
fi

# ──────────────────────────────────────────────────────────────────
# [2] Gerar carteira Ethereum (nao-interativo)
# ──────────────────────────────────────────────────────────────────
echo ""
echo "[2/4] Gerando/carregando carteira Ethereum..."

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd "$AUTOMATON_RUNTIME"
INIT_OUTPUT=$(node dist/index.js --init 2>&1 || true)

# Extrair endereco da saida JSON
WALLET_ADDRESS=$(echo "$INIT_OUTPUT" | python3 - << 'PYEOF'
import sys, json
for line in sys.stdin:
    line = line.strip()
    if line.startswith("{"):
        try:
            data = json.loads(line)
            if "address" in data:
                print(data["address"])
                sys.exit(0)
        except Exception:
            pass
PYEOF
)

# Fallback: ler wallet.json existente
if [ -z "$WALLET_ADDRESS" ] && [ -f "$AUTOMATON_DIR/wallet.json" ]; then
    WALLET_ADDRESS=$(python3 -c "
import json
with open('$AUTOMATON_DIR/wallet.json') as f:
    d = json.load(f)
print(d.get('address', ''))
" 2>/dev/null || true)
fi

WALLET_ADDRESS="${WALLET_ADDRESS:-0x0000000000000000000000000000000000000000}"
echo "  Carteira: $WALLET_ADDRESS"

# ──────────────────────────────────────────────────────────────────
# [3] Coletar informacoes do agente (interativo)
# ──────────────────────────────────────────────────────────────────
echo ""
echo "[3/4] Informacoes do agente..."
read -rp "  Nome do agente [AgentLaunch-HQ]: " AGENT_NAME
AGENT_NAME="${AGENT_NAME:-AgentLaunch-HQ}"
read -rp "  Seu endereco Ethereum (criador, 0x...) [Enter para pular]: " CREATOR_ADDRESS
CREATOR_ADDRESS="${CREATOR_ADDRESS:-0x0000000000000000000000000000000000000000}"

# ──────────────────────────────────────────────────────────────────
# [4] Escrever automaton.json e .env
# ──────────────────────────────────────────────────────────────────
echo ""
echo "[4/4] Escrevendo arquivos de configuracao..."

export BYOK_PROVIDER BYOK_KEY AGENT_NAME CREATOR_ADDRESS WALLET_ADDRESS
export INFERENCE_MODEL LOW_COMPUTE_MODEL AUTOMATON_DIR GENESIS_FILE

python3 - << 'PYEOF'
import json, os

provider       = os.environ["BYOK_PROVIDER"]
byok_key       = os.environ["BYOK_KEY"]
agent_name     = os.environ["AGENT_NAME"]
creator        = os.environ["CREATOR_ADDRESS"]
wallet         = os.environ["WALLET_ADDRESS"]
model          = os.environ["INFERENCE_MODEL"]
low_model      = os.environ["LOW_COMPUTE_MODEL"]
auto_dir       = os.environ["AUTOMATON_DIR"]
genesis_file   = os.environ["GENESIS_FILE"]

# Ler genesis prompt
if os.path.exists(genesis_file):
    with open(genesis_file) as f:
        genesis = f.read().strip()
else:
    genesis = (
        f"Voce e o {agent_name}, uma agencia de marketing autonoma B2A "
        "(Business-to-Agent). Seu objetivo e criar valor genuino para "
        "clientes agentes e humanos atraves de servicos de marketing digital, "
        "seguindo o Artigo I: nunca causar dano."
    )

config = {
    "name": agent_name,
    "genesisPrompt": genesis,
    "creatorAddress": creator,
    "registeredWithConway": False,
    "sandboxId": "byok-local",
    "conwayApiUrl": "https://api.conway.tech",
    # Valor nao-vazio e obrigatorio pela verificacao em src/index.ts:199.
    # As chamadas ao Conway API falham silenciosamente (graceful degradation).
    "conwayApiKey": "byok-placeholder",
    "inferenceModel": model,
    "maxTokensPerTurn": 4096,
    "heartbeatConfigPath": "~/.automaton/heartbeat.yml",
    "dbPath": "~/.automaton/state.db",
    "logLevel": "info",
    "walletAddress": wallet,
    "version": "0.2.1",
    "skillsDir": "~/.automaton/skills",
    "maxChildren": 3,
    "maxTurnsPerCycle": 25,
    "chainType": "evm",
    "modelStrategy": {
        "inferenceModel": model,
        "lowComputeModel": low_model,
        "criticalModel": low_model,
        "maxTokensPerTurn": 4096,
        "hourlyBudgetCents": 0,
        "sessionBudgetCents": 0,
        "perCallCeilingCents": 0,
        "enableModelFallback": True,
        "anthropicApiVersion": "2023-06-01",
    },
}

if provider == "anthropic":
    config["anthropicApiKey"] = byok_key
else:
    config["openaiApiKey"] = byok_key

# Escrever automaton.json (permissoes 600 — so o dono le)
config_path = os.path.join(auto_dir, "automaton.json")
with open(config_path, "w") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
os.chmod(config_path, 0o600)
print(f"  automaton.json escrito  ({len(genesis)} chars de genesis prompt)")

# Escrever .env para o supervisor carregar as chaves em tempo de execucao
env_path = os.path.join(auto_dir, ".env")
env_lines = [
    "# Chaves de API — BYOK (Bring Your Own Key)",
    "# NÃO compartilhe este arquivo.",
    f"ANTHROPIC_API_KEY={os.environ.get('ANTHROPIC_API_KEY', '')}",
    f"OPENAI_API_KEY={os.environ.get('OPENAI_API_KEY', '')}",
]
with open(env_path, "w") as f:
    f.write("\n".join(env_lines) + "\n")
os.chmod(env_path, 0o600)
print(f"  .env escrito            ({env_path})")
PYEOF

# Criar diretorios de skills (o runtime cria os arquivos SKILL.md na primeira execucao)
mkdir -p "$AUTOMATON_DIR/skills/conway-compute"
mkdir -p "$AUTOMATON_DIR/skills/conway-payments"
mkdir -p "$AUTOMATON_DIR/skills/survival"

# Remover DB de estado para limpar saldo Conway cacheado (-$0.01).
# Sem isso, o agent inicia em tier "dead" indefinidamente.
if [ -f "$AUTOMATON_DIR/state.db" ]; then
    rm -f "$AUTOMATON_DIR/state.db"
    echo "  state.db removido (saldo Conway cacheado apagado)."
fi

echo ""
echo "=============================================================="
echo " CONFIGURACAO BYOK CONCLUIDA!"
echo ""
echo "  Provedor : $BYOK_PROVIDER"
echo "  Modelo   : $INFERENCE_MODEL"
echo "  Carteira : $WALLET_ADDRESS"
echo ""
echo "  INICIAR o agente:"
echo "    cd ~/automaton && node dist/index.js --run"
echo ""
echo "  VERIFICAR status:"
echo "    cd ~/automaton && node dist/index.js --status"
echo ""
echo "  NOTA: chamadas ao Conway Cloud vao falhar com avisos (esperado)."
echo "  O agente usa apenas sua chave BYOK para inferencia."
echo "=============================================================="
