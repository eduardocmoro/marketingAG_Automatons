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

## RESULTADO DO DAY 1 — FECHADO

Medido em 8 execuções (coorte v3, `cu_consumed` por `simulateTransaction` em
100% das amostras, priority fee filtrada por `ammKey`). SOL a US$101,68.

### Break-even por tamanho de posição

| posição | break-even mediana | p90 |
|---|---|---|
| US$1 | **0,42%** | 1,17% |
| US$5 | ~0,085% | ~0,23% |
| US$50 | ~0,0085% | ~0,023% |

O custo de rede é **fixo em dólar** (~US$0,0043 por round trip na mediana), então
como % do notional ele explode quando a posição encolhe. Em US$1 ele responde por
**~98%** do break-even.

**Decisão: o tamanho operacional passa a ser US$5.** Em US$1 a taxa fixa consome
tudo e o gargalo deixa de ser o mercado.

### Componentes medidos

| componente | valor | fonte |
|---|---|---|
| `cu_consumed` | 68.807 por perna | `simulateTransaction` (não estimativa) |
| priority fee (pool) | p50 216.780 / p90 256.032 µlamp/CU | `getRecentPrioritizationFees` filtrado por `ammKey` |
| base fee | 5.000 lamports/assinatura | constante de protocolo |
| rent de ATA | 1.488.440 lamports = US$0,1514 | `getMinimumBalanceForRentExemption`, setup único |
| gap entre pernas | p50 142ms, p90 252ms | dentro do limite de 300ms |

A estimativa do roteador para `cu_consumed` era **145.299** — superestimava a real
em **2,1x**. E a priority fee medida com filtro de mints (1.000) estava **200x
abaixo** da real filtrada por `ammKey`. Os dois erros apontavam em direções
opostas; o líquido é custo de rede ~4x maior que a primeira leitura.

Um achado favorável: p90/p50 da fee = **1,18x**. Alta, mas **estável** — o custo é
caro e previsível, não caro e errático.

### O que NÃO foi medido: custo de spread

A perda de round trip saiu **negativa** em 7 de 8 tamanhos (−0,0081% em US$0,50,
encolhendo até +0,0027% em US$500). Ganho é impossível como custo.

Causa, confirmada pelas rotas observadas: **a perna 1 e a perna 2 quase nunca
passam pelo mesmo venue**. Em 8 execuções de US$0,50 houve 8 rotas distintas —
compra do Byreal e vende para o Quantum, compra do Flux e vende para o Byreal. O
roteador toma o melhor ask de um market maker e o melhor bid de outro, e o
agregado **cruza**. O efeito encolhe com o tamanho porque menos MMs cotam notional
grande e os dois lados se sobrepõem mais.

Consequências:

1. O método cotação-contra-cotação **não mede custo de spread** neste par.
2. Nenhum dos 16 venues declara `feeAmount` — o TESTE 3 fica NÃO APLICÁVEL.
3. Não existe rota estável, então agrupar tamanhos por rota (TESTE 1) não
   significa nada aqui.

**Isso não invalida o break-even acima**, porque o componente quebrado vale
−0,008% contra 0,42% de rede. Mas bloqueia medir custo em tamanhos grandes, onde o
spread passa a pesar, e bloqueia a Fase 2. Requer preço de referência
**independente da Jupiter** (oracle on-chain). Ainda não implementado.

### A pergunta que sobra

Não é mais "qual o custo" — está medido. É: **existe horizonte em que o movimento
previsível de SOL/USDC supera 0,085% em US$5?** Isso sai de histórico de preço, e
é o que o Day 2–3 responde.

---

## Day 1 — só medição de custo

Zero código de estratégia. O que é medido:

| Componente | Como |
|---|---|
| Fee de rede | `(base_fee × signatures) + (cu_price × cu_consumed)`; `cu_consumed` via `simulateTransaction` na tx real do Jupiter |
| Priority fee | `getRecentPrioritizationFees` **filtrado pelos `ammKey` da rota**, em p25/p50/p75/p90/p99 |
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

- rent por ATA: **0,00148844 SOL**, **recuperável** (volta integralmente no close)
- 5 mints do núcleo: ~0,00744 SOL travados, **uma vez** (~US$0,77 a SOL US$103)
- custo por trade: **zero** — não escala com o número de trades

#### `LAMPORTS_PER_BYTE_YEAR` é 2540, não 3480

A constante que eu usava estava errada. Três sondas independentes na mainnet
batem com **2540**, erro zero em todas:

```
  0 bytes : 128 × 2540 × 2 =   650.240   (medido   650.240)
 82 bytes : 210 × 2540 × 2 = 1.066.800   (medido 1.066.800)
165 bytes : 293 × 2540 × 2 = 1.488.440   (medido 1.488.440)
```

O script sonda os três tamanhos a cada execução e o **valor medido é a fonte de
verdade**; a fórmula só existe para o relatório flagrar se a rede mudar o
parâmetro.

Por isso o rent ficou fora de `RoundTripCost` e do cálculo de break-even. O
relatório converte pelo preço de SOL **medido na amostra**, nunca por constante.

### Drift de cotação: o custo do tempo

Custo estático (fee, impacto) é função do tamanho. Custo do tempo não é, e slot
time da Solana é ~350ms — a quote envelhece dentro do loop.

A medição cota em t0 e recota o **mesmo par e tamanho** após cada horizonte,
registrando o delta de `outAmount` em bps. Isso produz um **piso empírico de
slippage sem executar nada**.

#### `|drift|` não é custo aditivo

Drift é **sinalizado**, e num swap o `outAmount` favorável é capturado
integralmente. Somar o módulo trataria todo movimento como adverso — uma
penalidade de momentum aplicada inclusive a agentes de reversão à média, para quem
o mesmo drift é favorável.

O custo do drift é **condicional à direção do sinal do agente**, não constante
global. A distribuição **sinalizada** fica preservada em
`quoted_drift_signed_percentiles` e o custo é aplicado por agente na camada de
evolução.

#### O canal de custo é reversão

```
drift favorável                → capturado integralmente
adverso dentro da tolerância   → custo real, condicional ao sinal
adverso além da tolerância     → instrução falha, tx reverte,
                                 paga taxa de rede sem posição
```

Daí sai o que **é** global e mensurável na Fase 1: dada uma tolerância de
slippage, a distribuição de drift medida dá a probabilidade de estourá-la.

A tolerância limita **apenas o lado adverso**, então a probabilidade é
unilateral — `P(drift < −tol)` por perna, não `P(|drift| > tol)`. Para o round
trip, `1 − (1−p)²`.

Registrado como `revert_rate_floor_by_slippage_bps`, **separado de
`txFailureRate`**: falha por congestionamento, blockhash expirado ou saldo
reservado não devolvido vem de outros canais e não sai do drift. `txFailureRate`
segue `null` até a Fase 2.

O horizonte real de cada ponto fica entre `elapsedMsAtRequest` e
`elapsedMsAtResponse` — os dois são registrados em vez de assumir que a espera
nominal foi exata.

### Entrega do Day 1

1. **Tabela de custo determinístico** — `swapLoss + networkCost` por tamanho,
   mediana e p90. É o resultado sólido do Day 1.
2. **Calibração `quote_drift` vs sonda** — quanto do drift observado é movimento
   de preço e quanto é efeito de tamanho.
3. **Break-even determinístico em US$1** — a leitura direta.
4. **Curva de reversão** — marcada PRELIMINAR, fechada no Day 2–3 com histórico.

Break-even em função da **tolerância de slippage**, por tamanho de trade:

| Coluna | O que é |
|---|---|
| determinístico | `swapLoss + networkCost` |
| `P(revert)` | piso de reversão por perna, medido do drift |
| gás desperdiçado | `networkCost × p/(1−p)` por round trip bem-sucedido |
| BE sem sandwich | braço esquerdo — **medido** |
| sandwich máx | `posição × tol/10000` — teto aritmético, **não medido** |
| BE com sandwich | curva fechada, com mínimo |

A curva tem dois braços:

- **esquerdo** (tolerância apertada) → reversão, gás desperdiçado. **Medido.**
- **direito** (tolerância larga) → sandwich. Resolvido por **economia do
  atacante**, não por medição de MEV.

#### Limiar de triagem do searcher — estimativa mole, NÃO medida

**Não existe piso econômico por custo marginal de transação.** MEV na Solana opera
por bundle com **tip leiloado competitivamente**: o tip acompanha a extração, então
o custo do atacante sobe junto com o ganho e não forma piso fixo. Um searcher pode
dar lance em praticamente qualquer extração positiva.

O que de fato protege a posição pequena é o **limiar de triagem**: overhead fixo
por tentativa (infra, simulação, monitoramento) e o filtro de lucro mínimo que o
searcher aplica antes de olhar o alvo. Isso é **política de operação de terceiro,
não aritmética**, e não foi medido aqui.

```
extração(pos, tol) = pos × tol/10000
limiar(tol)        ≈ overhead_de_triagem × 10000 / tol
```

`mevTriageEstimate()` em `src/costs/model.ts` usa 2× a taxa de uma tx de swap
apenas como **proxy grosseiro** desse overhead. O overhead real é provavelmente
maior (infra e capital não aparecem numa taxa de rede), mas pode ser menor para
quem já roda a infra para outros alvos. O campo carrega `measured: false` e o JSON
nomeia o número `tolerance_at_triage_threshold_bps_UNMEASURED`.

**Uso correto:** ordem de grandeza, operando com **folga grande** sob o limiar,
nunca colado nele. Confirmação só com dado real na Fase 2.

#### Resolução de cauda: n bruto vs n efetivo

`P(revert)` é probabilidade de cauda. Observações de tamanhos dentro do mesmo
bloco são **contíguas no tempo**, e volatilidade forma cluster — então não são
independentes. O relatório mede a autocorrelação de lag 1 de `|drift|` na ordem de
medição e reporta os dois:

```
n_eff = n × (1 − ρ) / (1 + ρ)      (limitado a n: ρ < 0 não cria amostra nova)
```

A resolução de cauda sai do **n efetivo**, não do bruto. Tolerâncias onde nenhuma
amostra estourou aparecem com `<` — teto de `1/n_eff`, não zero.

**Não recalcule o n por raciocínio** — o relatório conta direto do `jsonl` e
imprime o valor real por horizonte.

### Divisão de papéis: polling vs histórico

Estender o polling para centenas de blocos é ineficiente — 16h de coleta para
algumas centenas de pontos. A cauda de drift adverso sai de **histórico de trades
SOL/USDC**, com ordens de magnitude mais amostras, e o data layer do Day 2–3 já
coleta isso.

| Fonte | n | Papel |
|---|---|---|
| polling (Day 1) | pequeno | calibrar `quote_drift` vs sonda de notional desprezível |
| histórico (Day 2–3) | grande | distribuição de cauda e `revert_rate_floor` definitivo |

Por isso a curva do Day 1 sai marcada **PRELIMINAR** e **não bloqueia nada**.

#### A sonda só vale com rota coincidente

Jupiter **roteia por tamanho**: uma sonda de US$0,10 pode sair single-hop enquanto
US$500 abre split multi-venue. Quando isso acontece, `sizeComponentBps` mistura
efeito de **rota** com efeito de **tamanho** e não mede nada.

A medição registra a assinatura de rota (pools concretos + split) das duas quotes
e grava `routeMatch`. A calibração **exclui** as observações com rota divergente e
o relatório informa quantas foram excluídas e em que proporção. Sonda degenerada
(sem rota ou sem saída) é descartada na origem.

Também é registrado `sizeRouteChangedFromBase`: quando a rota da própria quote de
tamanho muda entre `t0` e `t0+horizonte`, o "drift" daquele ponto carrega uma
descontinuidade de roteamento, não só movimento de preço. O relatório conta esses
casos separadamente.

Se a maioria das observações divergir, a sonda de US$0,10 não representa a liquidez
que os tamanhos reais tocam e precisa de outro notional — o relatório diz isso em
vez de calcular uma calibração inválida.

### `slippageBps` é parâmetro evoluído, não constante

Não existe valor global correto:

- **apertado** → mais reversão, gás desperdiçado em tentativas que falham
- **largo** → sandwich extrai até o limite autorizado

Entra no vetor de parâmetros do agente, ao lado de janela e limiar de entrada. A
grade em `SLIPPAGE_GRID_BPS` existe só para desenhar a curva — não é configuração
de produção.

Round trip é medido de ponta a ponta: entra com N USDC, a perna 2 usa exatamente
o `outAmount` da perna 1, sai com M USDC. `swapLoss = N − M` captura fee de pool e
price impact das duas direções sem modelar cada pool à mão.

### Taxa de pool é RESULTADO do Day 1, não premissa

A conversa inteira carregou "Raydium 0,25%, round trip 0,5%" como dado. **Isso
nunca foi medido — foi assumido.** O routePlan devolve `feeAmount` e `feeMint`
por salto, e daí sai o **tier efetivo do pool** que aquele tamanho realmente
atravessou:

```
feeMint == inputMint  →  tier = feeAmount / inAmount
feeMint == outputMint →  tier = feeAmount / outAmount
```

O relatório imprime o tier por (tamanho, perna, salto, venue).

#### Resultado medido: o custo em SOL/USDC não é AMM de taxa fixa

Em ~60 rotas observadas, **Raydium CLMM apareceu 2 vezes**. A Jupiter
praticamente não roteia SOL/USDC por AMM de 25 bps. A esmagadora maioria vai por
**venues de spread** — market makers e cotação privada (Byreal, Quantum,
HumidiFi, ZeroFi, SolFi, AlphaQ, GoonFi, Manifest, TesseraV, Kipseli, BisonFi,
Deriverse, Flux) — que **não declaram `feeAmount`**: o custo está embutido no
preço cotado como spread.

> **A premissa herdada de 0,25% não se aplica a este par.** O custo de pool em
> SOL/USDC via Jupiter é dominado por venues de spread, não por AMM de taxa fixa.

Isso **reforça a decomposição**: em US$1 a taxa de rede domina por margem ainda
maior que a calculada com 25 bps.

O relatório classifica cada venue (AMM com fee declarada vs spread) e reporta a
fração de saltos com `feeAmount > 0`.

#### Validador para venue de spread

`feeAmount = 0` não significa custo zero — significa que **não há taxa declarada
para validar contra**. O TESTE 3 fica **NÃO APLICÁVEL** nesse caso, suspenso, nem
aprovado nem reprovado.

O validador que falta: comparar `outAmount` contra preço de referência
**independente da Jupiter** — oracle on-chain (Pyth). Comparar quote contra quote
da mesma fonte é circular. **Ainda não implementado**; exige decidir conta de
preço, tratamento de staleness e intervalo de confiança do oracle.

Enquanto isso, o **TESTE 4** valida pelo outro lado e funciona em venue de
spread: a perda só é custo se for grande perto da **deriva de preço no mesmo
intervalo** entre as pernas. Razão < 3x é indistinguível de ruído.

**Mas tier baixo não salva o desenho de US$1.** Taxa de pool é percentual;
custo de rede é **fixo em dólar**. Em posição pequena o fixo domina:

| | US$0,50 | US$1 | US$5 | US$50 |
|---|---|---|---|---|
| rede (fixa ~US$0,001) | 0,21% | 0,10% | 0,021% | 0,002% |
| pool a 1 bps (round trip) | 0,02% | 0,02% | 0,02% | 0,02% |

Descobrir tier baixo reduz o componente que **já era menor**. O relatório
decompõe break-even em fixo vs percentual por tamanho e calcula o
**cruzamento**:

```
cruzamento = rede_usd × 100 / pool_pct
```

Esse ponto é o **tamanho mínimo economicamente racional** — abaixo dele o custo
é dominado por uma taxa que não diminui por mais que a ordem encolha. E tier
**menor** empurra o cruzamento para **cima**: com pool barato é preciso posição
maior antes que o pool passe a importar.

A tabela sai em **duas versões lado a lado**: rede na mediana e rede no p90 da
priority fee. A coluna p90 é a que descreve o pior caso operacional, e é ela que
decide o desenho — deixar só a mediana visível subestima o custo por ~8x. O
cruzamento também sai nas duas: com tier de 0,02%, ~US$5 na mediana e ~US$42 no
p90. **Um desenho que só fecha na coluna mediana não fecha em congestionamento.**

#### Priority fee: usar sempre a filtrada por pool

Medido em 21 execuções: **global p50/p90 = 0 / 0**, mas **filtrada por `ammKey`
p50 = 317.255**.

> Os pools de SOL/USDC estão entre as contas mais disputadas da rede **mesmo com
> a rede inteira calma**. Isso valida o filtro por `ammKey` e **invalida qualquer
> uso do global**.

O break-even usa **sempre a filtrada**. A global fica no relatório só como
contraste — e o contraste é o achado.

#### Detecção de cap na coleta

Percentil redondo demais é suspeito de teto, não de observação. O script grava a
forma da distribuição (valores distintos, empates no máximo, 5 valores de topo
com contagem) e o relatório alerta quando:

- mais de 10% das amostras empatam no valor máximo → assinatura de saturação
- p90/p99 é múltiplo exato de 100.000 → redondo demais para ser empírico

Sem isso, um teto na coleta entraria no break-even como se fosse cauda real.

### Segmentação por versão do código

O `jsonl` é append-only e acumula versões do script ao longo dos dias. Versões
diferentes **medem coisas diferentes** — a correção do gap entre pernas (schema
v3) mudou a semântica do round trip, e agregar v2 com v3 faz **dado velho
envenenar dado novo**.

Cada amostra grava `schemaVersion` e `gitSha`. O relatório segmenta e usa **só a
versão mais recente** para round trip, TESTE 0 e TESTE 4, reportando quantas
amostras ficaram de fora. As antigas permanecem no `jsonl` como histórico.

**A contaminação de gap é derivada do próprio `legGapMs`**, nunca do campo
gravado pelo script — amostras de versões anteriores não têm esse campo, e
tratá-las como limpas produzia contagem incoerente com a média.

#### Proveniência do `cu_consumed`

Toda a tabela de break-even depende desse número. O relatório reporta a fração de
amostras por `cuConsumedSource` e alerta quando **nenhuma** veio de
`simulateTransaction` — o RPC público recusa simulação, e nesse caso tudo é
estimativa do roteador. Se as duas fontes coexistirem e divergirem mais de 10%, o
relatório avisa que o break-even muda conforme a fonte.

### Decisão de desenho: abstenção por priority fee

*(registrado agora, implementado no Day 4–5)*

A cauda de priority fee **não é custo inevitável**. A fee é **observável antes de
assinar** — o agente consulta `getRecentPrioritizationFees` e decide. Logo a cauda
é **critério de abstenção**, não custo esperado: um agente que recusa operar acima
de um limiar paga próximo da mediana na maior parte do tempo.

Isso entra no vetor de parâmetros como **`max_priority_fee_to_trade`**, evoluído
por agente ao lado de `slippageBps`:

- **limiar baixo** → paga perto da mediana, mas opera menos
- **limiar alto** → opera sempre, mas come a cauda

**Ressalva que precisa estar escrita para não virar otimismo silencioso:** se o
sinal aparecer preferencialmente em janelas congestionadas — e volatilidade e
congestionamento **correlacionam** — então o filtro corta sinal junto com custo, e
o ganho líquido da abstenção pode ser zero ou negativo. Um agente que só opera no
mercado calmo pode estar recusando exatamente as janelas em que havia edge.

Isso é **mensurável no Day 2–3** com histórico (correlação entre priority fee e
magnitude de movimento de preço na mesma janela), **não agora**. Até lá,
`max_priority_fee_to_trade` é hipótese de desenho, não solução.

#### Ressalva: tier de CLMM vale para o tamanho medido

Pool de 1–5 bps é **liquidez concentrada em faixa estreita**. Fora da faixa o
impacto de preço sobe rápido. Irrelevante em US$1; relevante se o enxame
escalar notional. **O tier observado é válido para o tamanho medido e não é
extrapolável para volume maior** — reescalar exige remedir.

### Integridade antes de leitura

**Jupiter roteia por tamanho.** Cada tamanho pode atravessar venue e split
diferentes, e comparar perda entre rotas distintas mistura taxa de venue com
impacto de tamanho. O relatório agrupa os tamanhos por assinatura de rota
(pools concretos + split) **antes** de qualquer teste.

1. **TESTE 3 — validação cruzada (PRIMÁRIO).** A soma de `feeAmount` do
   `routePlan` das duas pernas tem que ser coberta pela perda observada. É o
   **único teste independente de rota**, porque compara contra a taxa daquele
   caminho específico. Perda observada menor que a taxa que o próprio roteador
   diz ter cobrado é fisicamente impossível — aí o bug é do nosso cálculo.
2. **TESTE 1 — monotonicidade (SECUNDÁRIO).** Só se aplica **dentro** do
   subconjunto de tamanhos que compartilham a mesma assinatura de rota. Entre
   rotas diferentes, não-monotonicidade **não é evidência de bug**.
3. **TESTE 2 — piso físico.** Perda abaixo de 0,02% é sinalizada, mas se a taxa
   efetiva medida confirmar tier baixo, **não é bug — é venue barato**. Quem
   decide é o TESTE 3.

As duas quotes do round trip são disparadas **coladas no tempo**, e `legGapMs`
registra o intervalo. Construir a tx de swap e simular entre as pernas metia
0,5–2s de deriva de preço dentro da medição.

**Convenção de sinal, explícita:** `roundTripReturnPct < 0` é perda,
`swapLossPct > 0` é perda. Um é o negativo do outro.

### Segurança da chave de RPC

A `SOLANA_RPC_URL` de um RPC dedicado carrega a API key. Ela **nunca** é
impressa no stdout nem gravada no `jsonl` — só host e os 4 últimos caracteres.
O `jsonl` é commitado como trilha de auditoria, então a chave completa ali
seria vazamento permanente no histórico do git.

### Rodar

```bash
cd taios-swarm

# RPC dedicado é fortemente recomendado — o público recusa simulateTransaction
export SOLANA_RPC_URL='https://mainnet.helius-rpc.com/?api-key=SUA_CHAVE'

bash scripts/run-day1.sh            # 1 amostra + relatório
bash scripts/run-day1.sh 8 300      # 1 bloco: 8 amostras a cada 5 min

# se aparecer 429 da Jupiter, aumente o espaçamento
JUP_MIN_GAP_MS=2000 bash scripts/run-day1.sh 8 300
```

O free tier da Jupiter (`lite-api`) derruba com 429. O script aplica throttle
global (padrão 1200ms entre chamadas) e backoff exponencial, registra **toda**
ocorrência de 429 em `rateLimitEvents`, e marca a amostra como incompleta —
**dado parcial nunca sai silencioso**. O drift roda sem throttle interno para
não destruir os horizontes, compensando com folga entre tamanhos, e mede apenas
os 4 tamanhos do núcleo (é movimento de preço, quase independente do tamanho).

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
