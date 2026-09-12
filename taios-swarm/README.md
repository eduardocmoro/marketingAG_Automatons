# TAIOS-Swarm

Enxame evolutivo de micro-arbitragem em Solana.

**Pergunta que o projeto existe para responder (Fase 1):** algum agente consegue,
de forma consistente, superar o custo total de round trip? Objetivo financeiro só
depois que houver edge validado em paper.

**Fase 1 é 100% paper.** Nenhuma linha de execução real é ativada sem OK explícito.

---

## Regras invioláveis

1. **Nenhum número inventado.** Fee, slippage e latência vêm de medição ou fonte
   verificada. Faltou medir, o campo fica `null` e a fonte da falha é registrada.
2. **Nunca afirmar que uma estratégia "vai dar lucro".** Só expectativa líquida com
   intervalo de confiança e tamanho de amostra.
3. **Chave privada nunca no repositório, nunca em commit, nunca em log.**
4. **Uma hot wallet compartilhada, agentes virtuais.** Nunca um keypair por
   agente. Decisão irreversível — ver [ADR-001](docs/ADR-001-carteira-unica.md).

---

## Critério de aprovação (emenda 1)

Não basta net_EV positivo. Para um agente ser considerado validado:

**a) Split temporal**
Evolução e seleção rodam **somente na primeira metade** dos dados. Validação na
segunda metade, **uma única passagem, sem re-seleção**. Reotimizar na metade de
validação invalida o resultado.

**b) População de controle**
A mesma arquitetura roda sobre retornos embaralhados (ou sinal de entrada
aleatório). O melhor agente real precisa **superar o melhor agente do controle** —
não basta superar zero. Com N agentes e seleção, o melhor de qualquer população
parece bom por sorte; o controle mede exatamente esse viés.

**c) Reporte obrigatório**
Todo resultado sai com: nº de agentes testados, nº de sobreviventes, net_EV com IC,
e o net_EV do melhor do controle. Sem esses quatro números o resultado não é
interpretável e não deve ser reportado.

## Classificador de regime (emenda 2)

Cada janela é rotulada como lateral/tendência e por faixa de volatilidade.
Promoção para capital real exige net_EV positivo em **≥ 2 regimes distintos**.
Vencedor de regime único é **candidato, não campeão**.

## Separação slippage (emenda 3)

Quatro conceitos distintos que **nunca se misturam**:

| Campo | Função de | Fase | Origem |
|---|---|---|---|
| `quotedPriceImpactPct` | **tamanho** | 1 e 2 | `priceImpactPct` da quote |
| `quotedDriftBpsByLatency` | **tempo** | 1 e 2 | recotação após cada horizonte |
| `realizedSlippagePct` | execução | **2 apenas** | cotado vs. obtido on-chain |
| `txFailureRate` | execução | **2 apenas** | fração de tx que falharam |

Os dois últimos permanecem `null` durante todo o paper trading, e o relatório
verifica isso a cada execução. `slippageDivergence()` e
`effectiveCostWithFailures()` em `src/costs/model.ts` lançam erro se alguém tentar
usá-los na Fase 1 — sem execução real esses números não existem, e estimá-los
seria inventar.

Motivo do `txFailureRate` existir já: pelo [ADR-001 §2](docs/ADR-001-carteira-unica.md),
falha mal contabilizada é a contaminação mais perigosa do modelo. Uma execução que
falha por saldo reservado não devolvido **parece slippage adverso**, envenena a
métrica que o kill switch observa, e faz o enxame matar agentes bons. Na Fase 2 a
falha entra no break-even como custo, não como ruído.

## Fronteira de linguagem (emenda 4)

```
TypeScript  →  shell, loop, execução, medição       →  escreve SQLite
                                    SQLite = fronteira
Python      →  distribuição, IC, teste contra controle, regime  →  lê SQLite
```

Estatística não é reimplementada em TS.

---

## Day 1 — só medição de custo

Zero código de estratégia. O que é medido:

| Componente | Como |
|---|---|
| Fee de rede | `(base_fee × signatures) + (cu_price × cu_consumed)`; `cu_consumed` via `simulateTransaction` na tx real do Jupiter |
| Priority fee | `getRecentPrioritizationFees`, distribuição (mediana e p90), não ponto único |
| Fee de pool | `routePlan` real da quote — venue, `feeAmount`, `feeMint`, nº de hops por salto. Nada fixado em 0,25% |
| Price impact cotado | `priceImpactPct` da quote, **nas duas direções** (assimétrico em CLMM) |
| Drift por latência | mesma quote recotada após 250ms / 500ms / 1s / 2s / 5s |
| Rent de ATA | `getMinimumBalanceForRentExemption(165)` — custo de **setup, uma vez**, não por trade |

Tamanhos: **0,50 / 1 / 2 / 5** USDC (núcleo da tese) + 10 / 50 / 100 / 500 (escala).

### Rent de ATA não é custo por trade

Uma ATA é criada **uma vez por (carteira, mint)** e reusada por todos os trades
seguintes, de todos os agentes. Sob [ADR-001](docs/ADR-001-carteira-unica.md) o
enxame inteiro usa uma carteira só, então são **N ATAs no total, para sempre** —
uma por mint negociado, não uma por posição nem uma por agente.

- rent por ATA: 0,00204 SOL, **recuperável** (volta integralmente no close)
- 5 mints do núcleo: ~0,0102 SOL travados, **uma vez**
- custo por trade: **zero** — não escala com o número de trades

Por isso o rent ficou fora de `RoundTripCost` e do cálculo de break-even. O
relatório converte pelo preço de SOL **medido na amostra**, nunca por constante.

### Drift de cotação: o custo do tempo

Custo estático (fee, impacto) é função do tamanho. Custo do tempo não é, e slot
time da Solana é ~350ms — a quote envelhece dentro do loop.

A medição cota em t0 e recota o **mesmo par e tamanho** após cada horizonte,
registrando o delta de `outAmount` em bps. Isso produz um **piso empírico de
slippage sem executar nada**.

A mediana com sinal tende a zero (o preço anda para os dois lados); o que custa é
a **magnitude**, porque metade das vezes ela joga contra. O break-even sensível a
latência usa essa magnitude:

```
break_even = swapLoss + networkCost + |drift(latência)|
```

O relatório imprime a coluna sem drift e com drift em 1s e 2s, para mostrar quanto
a velocidade do loop custa ao desenho.

O horizonte real de cada ponto fica entre `elapsedMsAtRequest` e
`elapsedMsAtResponse` — os dois são registrados em vez de assumir que a espera
nominal foi exata.

Round trip é medido de ponta a ponta: entra com N USDC, a perna 2 usa exatamente
o `outAmount` da perna 1, sai com M USDC. `swapLoss = N − M` captura fee de pool e
price impact das duas direções sem modelar cada pool à mão.

### Rodar

```bash
cd taios-swarm

# RPC dedicado é fortemente recomendado — o público recusa simulateTransaction
export SOLANA_RPC_URL='https://mainnet.helius-rpc.com/?api-key=SUA_CHAVE'

bash scripts/run-day1.sh            # 1 amostra + relatório
bash scripts/run-day1.sh 8 300      # 1 bloco: 8 amostras a cada 5 min
```

**Um bloco não fecha o Day 1.** Oito amostras seguidas cobrem ~40 minutos: um
único regime de congestionamento. O mínimo é **3 blocos em horários distintos ao
longo de 2 dias**, acumulando no mesmo `measurements/day1_costs.jsonl`
(append-only).

O relatório aplica **dois gates**, e ambos precisam passar:

1. **Cobertura temporal** — ≥ 3 janelas em ≥ 24h.
2. **Variação de congestionamento** — a janela mais congestionada precisa ter ao
   menos 2× o priority fee mediano da mais calma.

O segundo existe porque três janelas em horário morto passam no primeiro e
**mentem no p90**. O próprio priority fee é o medidor de atividade on-chain, então
o gate é medido, não presumido. Pelo menos um bloco precisa cair em período de
alta atividade — na prática, o horário de mercado dos EUA (~13:00–21:00 UTC)
costuma ser mais movimentado que a madrugada.

Cada bloco leva ~40s a mais por causa da medição de drift (5s de horizonte por
tamanho de trade).

Relatório isolado:

```bash
python3 analysis/day1_report.py
```

### Requisitos de rede

Estes hosts precisam estar liberados:

- `lite-api.jup.ag` (ou `quote-api.jup.ag` / `api.jup.ag`)
- o host do seu RPC Solana

### Sobre `MEASURE_PUBKEY`

Para obter `unitsConsumed` é preciso montar a tx de swap, e o Jupiter exige um
`userPublicKey`. A conta é usada **somente em `simulateTransaction`** — nada é
assinado, nada é enviado à rede, nenhuma chave privada está envolvida. O padrão é
um endereço público conhecido com saldo de SOL e USDC, para a simulação não abortar
por falta de fundos. Sobrescreva com `MEASURE_PUBKEY` se preferir outro.

---

## Estrutura

```
taios-swarm/
  docs/
    ADR-001-carteira-unica.md   decisão irreversível de arquitetura
  scripts/
    measure-day1.mjs      medição (sem estratégia)
    run-day1.sh           acumula amostras + relatório
  src/
    costs/model.ts        modelo de custo; quoted vs realized separados
  analysis/
    day1_report.py        JSONL -> SQLite -> tabela de custo + break-even
  measurements/
    day1_costs.jsonl      amostras brutas (append-only)
    day1.sqlite           base analítica
    day1_summary.json     resumo agregado
```

## Cronograma

| Dia | Entrega |
|---|---|
| 1 | Custo real medido. Tabela de round trip por tamanho, mediana e p90. **Sem código de estratégia.** |
| 2–3 | Data layer: WebSocket + candles em SQLite (SOL/USDC, JUP, RAY) |
| 4–5 | Cost model fechado + **ledger virtual** (ADR-001 §1 e §2) + um agente paper (vetor único), 48h de paper |
| 6–8 | População + evolução + classificador de regime + população de controle |
| 9+ | Split temporal, validação em passagem única, relatório com os 4 números obrigatórios |
