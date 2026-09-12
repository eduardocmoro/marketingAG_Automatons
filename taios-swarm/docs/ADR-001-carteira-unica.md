# ADR-001 — Uma hot wallet compartilhada, agentes virtuais

**Status:** aceito, **irreversível**
**Data:** 2026-09-12

## Decisão

O enxame inteiro opera sobre **uma única hot wallet** (um keypair).
Agentes são **entidades virtuais** sobre essa carteira, com posição e P&L
rastreados num ledger interno.

**NUNCA um keypair por agente.** Esta decisão não é revisitada sem refazer
a análise de custo do zero.

## Razão

Com N agentes e M mints negociados:

| Arquitetura | ATAs | Reserva de SOL |
|---|---|---|
| keypair por agente | N × M | N × (reserva mínima) |
| carteira única | M | 1 × (reserva mínima) |

Com 100 agentes e 5 mints do núcleo: 500 ATAs contra 5. O rent de ATA é
~0,00204 SOL por conta. A diferença é ~1,02 SOL travados contra ~0,0102 SOL.

A banca inicial do projeto não sobrevive à primeira arquitetura. Ela seria
consumida em rent e reserva antes do primeiro trade — sem que uma única
hipótese de estratégia fosse testada.

## Consequências

### 1. Ledger virtual obrigatório

A carteira tem **apenas saldo agregado**. On-chain não existe "posição do
agente 47". Toda atribuição de posição e P&L por agente vive num ledger
interno, e esse ledger é a única fonte de verdade sobre desempenho
individual.

Invariante que o ledger precisa manter:

```
Σ (posições virtuais de todos os agentes)  ==  saldo real da carteira
```

Divergência entre os dois lados é bug de contabilidade, e o kill switch
deve tratá-la como tal — não como P&L.

### 2. Reserva de saldo antes do trade

Este é o ponto de concorrência que **de fato** existe, e não é o keypair.

Transações Solana não têm nonce sequencial: várias transações do mesmo
keypair podem estar em voo ao mesmo tempo e pousar em qualquer ordem. O
blockhash não as serializa. **Concorrência de assinatura não é problema.**

O problema real é **saldo compartilhado**: se dois agentes virtuais
decidem gastar o mesmo USDC, um dos dois falha na execução. O ledger
precisa **reservar** o saldo no momento da decisão e liberar na liquidação
ou no cancelamento. Sem reserva, o enxame gera falhas de execução que o
modelo lê erroneamente como slippage.

### 3. Rent de ATA sai do custo por trade

ATA é criada **uma vez por (carteira, mint)** e reusada por todos os trades
seguintes, de todos os agentes. Com carteira única, o custo é:

- **uma vez**, no setup: M ATAs × 0,00204 SOL — **capital travado recuperável**
- **por trade**: zero rent adicional

Só a taxa de tx de criação é afundada, e amortizada sobre todos os trades
futuros ela tende a zero. O rent não entra na conta de break-even por trade.

### 4. Atribuição de custo entre agentes

Fee de rede e perda de swap são reais e pertencem ao trade que os causou.
O ledger atribui cada custo ao agente que originou a ordem. Custo de setup
não é rateado entre agentes — é do portfólio.

## Riscos aceitos

- **Ponto único de falha**: comprometer a carteira compromete tudo. Mitigação:
  a hot wallet contém apenas capital de operação, e a chave privada nunca
  entra no repositório, em commit ou em log.
- **Bug de ledger vira perda real**: um erro de contabilidade permite
  sobre-alocação. Mitigação: a invariante da seção 1 é verificada a cada
  ciclo contra o saldo on-chain, e divergência para o enxame.
