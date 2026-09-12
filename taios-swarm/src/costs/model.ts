/**
 * TAIOS-Swarm — Modelo de Custo
 *
 * Módulo de primeira classe. Nenhum agente é avaliado sem passar por aqui.
 *
 * SEPARAÇÃO INVIOLÁVEL (emenda 3):
 *   quotedPriceImpactPct  → cotado pelo roteador. Disponível na Fase 1.
 *   realizedSlippagePct   → medido na execução real. SEMPRE null na Fase 1.
 * Os dois nunca se misturam, nunca um preenche o outro, e o kill switch da
 * Fase 2 compara exatamente um contra o outro.
 */

/** Fase 1 = paper. Fase 2 = execução real. */
export type Phase = "paper" | "live";

/** Constantes de protocolo Solana (fixas, verificáveis on-chain). */
export const BASE_FEE_LAMPORTS_PER_SIGNATURE = 5_000;
export const LAMPORTS_PER_SOL = 1_000_000_000;
export const SPL_TOKEN_ACCOUNT_BYTES = 165;

/** De onde veio o cu_consumed. Nunca "estimado de cabeça". */
export type ComputeUnitSource = "simulation" | "jupiter_estimate" | "unavailable";

export interface ComputeMeasurement {
  cuConsumed: number | null;
  cuConsumedSource: ComputeUnitSource;
  signatures: number | null;
  simError: string | null;
}

/**
 * Custo de rede decomposto. total = base + priority.
 * priority = ceil(cuPriceMicroLamports * cuConsumed / 1e6)
 */
export interface NetworkFee {
  baseLamports: number;
  priorityLamports: number;
  totalLamports: number;
  cuPriceMicroLamports: number;
  cuConsumed: number;
  signatures: number;
}

/** Um salto do routePlan real devolvido pelo roteador (emenda 5). */
export interface RouteHop {
  venue: string | null;
  ammKey: string | null;
  inputMint: string | null;
  outputMint: string | null;
  inAmount: string | null;
  outAmount: string | null;
  /** Fee cobrada pelo pool, em unidades brutas de feeMint. Do roteador, não fixada. */
  feeAmount: string | null;
  feeMint: string | null;
  percent: number | null;
}

export interface Route {
  hops: number;
  legs: RouteHop[];
}

/** Uma perna do round trip. */
export interface LegCost {
  direction: "USDC->SOL" | "SOL->USDC";
  inAmountRaw: string;
  outAmountRaw: string;

  /** FASE 1: cotado pelo roteador antes de executar. */
  quotedPriceImpactPct: number | null;

  /**
   * FASE 2 APENAS: diferença entre o preço cotado e o preço efetivamente
   * obtido on-chain. Permanece null durante todo o paper trading.
   */
  realizedSlippagePct: number | null;

  /**
   * PARÂMETRO EVOLUÍDO POR AGENTE — não constante global.
   *
   *   apertado -> mais reversão, gás desperdiçado em tentativas que falham
   *   largo    -> sandwich extrai até o limite autorizado, e aí o drift
   *               deixa de ser simétrico e vira adversarial
   *
   * Faz parte do vetor de parâmetros do agente, como janela e limiar de
   * entrada. A curva de custo por tolerância tem mínimo, e o mínimo é o que
   * a evolução procura.
   */
  slippageBps: number | null;
  otherAmountThreshold: string | null;
  route: Route;
  compute: ComputeMeasurement;
  networkFee: NetworkFee | null;
}

/**
 * Envelhecimento de cotação por latência — o custo do TEMPO.
 *
 * Medido no Day 1 recotando o mesmo par e tamanho após cada horizonte.
 * É um piso empírico de slippage obtido sem executar nada.
 *
 * Três conceitos distintos que NUNCA se misturam:
 *   quotedPriceImpactPct     função do TAMANHO      Fase 1
 *   quotedDriftBpsByLatency  função do TEMPO        Fase 1
 *   realizedSlippagePct      execução real          Fase 2
 */
export interface DriftPoint {
  nominalHorizonMs: number;
  /** Horizonte real fica entre estes dois — o servidor cota em algum ponto no meio. */
  elapsedMsAtRequest: number;
  elapsedMsAtResponse: number | null;
  /** > 0 = receberia mais (a favor). < 0 = receberia menos (contra). */
  driftBps: number | null;
}

export interface QuotedDriftByLatency {
  positionSizeUsdc: number;
  baseOutAmount: string | null;
  baseQuoteAtUtc: string | null;
  points: DriftPoint[];
}

/**
 * Custo de setup do portfólio — UMA VEZ, nunca por trade.
 *
 * Uma ATA é criada uma vez por (carteira, mint) e reusada por todos os trades
 * seguintes, de todos os agentes. Sob ADR-001 (carteira única), são `mints`
 * ATAs no total para o enxame inteiro, para sempre.
 *
 * O rent é CAPITAL TRAVADO RECUPERÁVEL — volta integralmente ao fechar a conta.
 * Só a taxa de tx de criação é afundada, e amortizada sobre todos os trades
 * futuros ela tende a zero.
 *
 * Nada disto entra no break-even por trade. Ver ADR-001 §3.
 */
export interface PortfolioSetupCost {
  /** Quantos mints distintos o enxame negocia. */
  mints: number;
  rentLamportsPerAta: number;
  /** mints × rentLamportsPerAta. Travado, recuperável. */
  totalRentLamports: number;
  totalRentUsdc: number | null;
  /** Afundado, uma vez por ATA. */
  createTxFeeLamports: number | null;
  createTxFeeUsdc: number | null;
  /** true — o rent retorna integralmente no close. */
  rentRecoverable: true;
}

/**
 * Custo total de um round trip.
 *
 * A medida honesta de fee+impacto: começa com N USDC, termina com M USDC.
 * swapLossUsdc = N - M captura fee de pool e price impact das duas direções
 * sem precisar modelar cada pool separadamente.
 */
export interface RoundTripCost {
  phase: Phase;
  timestampUtc: string;
  positionSizeUsdc: number;
  solUsdcQuotedPrice: number | null;

  legs: [LegCost, LegCost];

  startUsdc: number;
  endUsdc: number;
  /** N - M. Fee de pool + price impact cotado, das duas pernas. */
  swapLossUsdc: number;
  swapLossPct: number;

  /** Soma das taxas de rede das duas pernas, convertida a USDC. */
  networkCostUsdc: number | null;

  /**
   * swapLoss + networkCost. Só isto.
   * Rent de ATA NÃO entra aqui: é setup de portfólio, uma vez por mint,
   * recuperável, e não escala com o número de trades (ADR-001 §3).
   */
  totalSunkCostUsdc: number | null;
  totalSunkCostPct: number | null;

  /**
   * FASE 2 APENAS. Fração de transações enviadas que falharam.
   *
   * Permanece null durante todo o paper trading — sem execução real não há
   * falha para contar, e estimá-la seria inventar número.
   *
   * Por que precisa existir já: pelo ADR-001 §2, falha mal contabilizada é
   * a contaminação mais perigosa do modelo. Uma execução que falha por
   * saldo reservado não devolvido parece slippage adverso, envenena a
   * métrica que o kill switch observa, e faz o enxame matar agentes bons.
   * Na Fase 2 a falha entra no break-even como CUSTO, não como ruído.
   */
  txFailureRate: number | null;

  /**
   * Movimento de preço necessário só para empatar, em %.
   * Igual a totalSunkCostPct por construção — nomeado à parte porque é
   * esta a leitura que decide se o projeto tem chance.
   */
  breakEvenMovePct: number | null;
}

// ── Cálculo ──────────────────────────────────────────────────────────

export function computeNetworkFee(
  signatures: number,
  cuPriceMicroLamports: number,
  cuConsumed: number
): NetworkFee {
  const baseLamports = BASE_FEE_LAMPORTS_PER_SIGNATURE * signatures;
  const priorityLamports = Math.ceil((cuPriceMicroLamports * cuConsumed) / 1_000_000);
  return {
    baseLamports,
    priorityLamports,
    totalLamports: baseLamports + priorityLamports,
    cuPriceMicroLamports,
    cuConsumed,
    signatures,
  };
}

export function lamportsToUsdc(lamports: number, solUsdcPrice: number): number {
  return (lamports / LAMPORTS_PER_SOL) * solUsdcPrice;
}

/**
 * Monta o custo consolidado. Retorna campos null onde a medição faltou —
 * nunca substitui por uma estimativa silenciosa.
 */
export function consolidate(params: {
  phase: Phase;
  timestampUtc: string;
  positionSizeUsdc: number;
  solUsdcQuotedPrice: number | null;
  legs: [LegCost, LegCost];
  startUsdc: number;
  endUsdc: number;
}): RoundTripCost {
  const { phase, timestampUtc, positionSizeUsdc, solUsdcQuotedPrice, legs, startUsdc, endUsdc } =
    params;

  const swapLossUsdc = startUsdc - endUsdc;
  const swapLossPct = (swapLossUsdc / startUsdc) * 100;

  const netLamports =
    legs[0].networkFee && legs[1].networkFee
      ? legs[0].networkFee.totalLamports + legs[1].networkFee.totalLamports
      : null;

  const networkCostUsdc =
    netLamports != null && solUsdcQuotedPrice != null
      ? lamportsToUsdc(netLamports, solUsdcQuotedPrice)
      : null;

  const totalSunkCostUsdc = networkCostUsdc != null ? swapLossUsdc + networkCostUsdc : null;

  const totalSunkCostPct =
    totalSunkCostUsdc != null ? (totalSunkCostUsdc / positionSizeUsdc) * 100 : null;

  return {
    phase,
    timestampUtc,
    positionSizeUsdc,
    solUsdcQuotedPrice,
    legs,
    startUsdc,
    endUsdc,
    swapLossUsdc,
    swapLossPct,
    networkCostUsdc,
    totalSunkCostUsdc,
    totalSunkCostPct,
    breakEvenMovePct: totalSunkCostPct,
    txFailureRate: null, // Fase 2 preenche; nunca estimado no paper
  };
}

/**
 * O canal de custo do drift é REVERSÃO, não degradação de fill.
 *
 *   drift favorável              -> capturado integralmente pelo swap
 *   adverso dentro da tolerância -> custo real, mas condicional ao sinal
 *   adverso além da tolerância   -> instrução falha, tx reverte,
 *                                   paga taxa de rede sem posição
 *
 * Por isso |drift| NÃO entra como custo aditivo no break-even global: somar
 * o módulo trata todo movimento como adverso, o que é uma penalidade de
 * momentum aplicada até a agentes de reversão à média, para quem o mesmo
 * drift é favorável. O custo do drift é CONDICIONAL À DIREÇÃO DO SINAL e
 * pertence à camada de evolução, por agente — não a esta constante global.
 *
 * O que é global e mensurável na Fase 1 é a reversão: dada uma tolerância,
 * a distribuição de drift medida dá a probabilidade de estourá-la.
 */
export interface RevertRateFloor {
  slippageToleranceBps: number;
  latencyHorizonMs: number;
  /** P(drift adverso além da tolerância) numa perna. */
  pAdverseSingleLeg: number;
  /** 1 − (1−p)²: ao menos uma das duas pernas reverte. */
  pRoundTrip: number;
  samples: number;
  /**
   * true quando nenhuma amostra estourou a tolerância. Nesse caso p é um
   * TETO de 1/n, não zero — a amostra só não resolve essa cauda.
   */
  isUpperBound: boolean;
}

/**
 * Custo esperado por round trip BEM-SUCEDIDO, dada a taxa de reversão.
 *
 * Cada perna precisa de 1/(1−p) tentativas em média, e cada tentativa
 * revertida paga taxa de rede sem gerar posição:
 *
 *   E[custo] = swapLoss + networkCost / (1 − p)
 *
 * Não inclui o adverso-dentro-da-tolerância: esse é condicional ao sinal do
 * agente e entra na camada de evolução, não aqui.
 */
export function expectedCostWithReverts(params: {
  swapLossUsdc: number;
  networkCostUsdc: number;
  pRevertPerLeg: number;
}): { expectedUsdc: number; wastedGasUsdc: number } {
  const { swapLossUsdc, networkCostUsdc, pRevertPerLeg: p } = params;
  if (p < 0 || p >= 1) throw new Error(`pRevertPerLeg fora de [0,1): ${p}`);
  const wastedGasUsdc = networkCostUsdc * (p / (1 - p));
  return { expectedUsdc: swapLossUsdc + networkCostUsdc + wastedGasUsdc, wastedGasUsdc };
}

/**
 * Teto de extração por sandwich: no limite, um atacante extrai toda a
 * tolerância autorizada. NÃO é medição — é o pior caso aritmético.
 */
export function sandwichUpperBoundUsdc(positionUsdc: number, slippageBps: number): number {
  return positionUsdc * (slippageBps / 10_000);
}

/**
 * Estimativa MOLE de limiar de triagem do searcher. NÃO É MEDIÇÃO.
 *
 * Não existe "piso econômico" por custo marginal de transação. MEV na Solana
 * opera por bundle com tip leiloado competitivamente: o tip ACOMPANHA a
 * extração, então o custo do atacante sobe junto com o ganho e não forma
 * piso fixo. Um searcher pode dar lance em praticamente qualquer extração
 * positiva.
 *
 * O que de fato protege a posição pequena é o LIMIAR DE TRIAGEM: overhead
 * fixo por tentativa (infra, simulação, monitoramento) e o filtro de lucro
 * mínimo que o searcher aplica antes de olhar o alvo. Isso é política de
 * operação de terceiro, não aritmética — e não foi medido aqui.
 *
 *   extração(pos, tol) = pos × tol/10000
 *   limiar(tol)        ≈ overhead_de_triagem × 10000 / tol
 *
 * `searcherOverheadProxyUsdc` usa 2× a taxa de uma tx de swap apenas como
 * PROXY grosseiro desse overhead. O overhead real é provavelmente maior
 * (infra e capital não aparecem numa taxa de rede), o que empurraria o
 * limiar para cima — mas também pode ser menor para quem já roda a infra
 * para outros alvos.
 *
 * Uso correto: operar com FOLGA GRANDE sob o limiar, nunca colado nele, e
 * tratar o número como ordem de grandeza. Confirmação só com dado real.
 */
export interface MevTriageEstimate {
  slippageToleranceBps: number;
  /** Proxy do overhead fixo de triagem. NÃO é o custo marginal do atacante. */
  searcherOverheadProxyUsdc: number;
  /** Posição abaixo da qual a extração provavelmente não passa na triagem. */
  triageThresholdPositionUsdc: number;
  /** true quando a posição fica sob o limiar estimado. Estimativa, não garantia. */
  positionBelowTriageEstimate: boolean;
  /** Sempre false na Fase 1 — nada aqui foi medido contra searchers reais. */
  measured: false;
}

export function mevTriageEstimate(params: {
  positionUsdc: number;
  slippageBps: number;
  /** Taxa de rede de UMA tx de swap, em USDC — proxy do overhead de triagem. */
  swapTxCostUsdc: number;
}): MevTriageEstimate {
  const { positionUsdc, slippageBps, swapTxCostUsdc } = params;
  const searcherOverheadProxyUsdc = 2 * swapTxCostUsdc;
  const triageThresholdPositionUsdc = (searcherOverheadProxyUsdc * 10_000) / slippageBps;
  return {
    slippageToleranceBps: slippageBps,
    searcherOverheadProxyUsdc,
    triageThresholdPositionUsdc,
    positionBelowTriageEstimate: positionUsdc < triageThresholdPositionUsdc,
    measured: false,
  };
}

/**
 * FASE 2: custo efetivo quando parte das transações falha por canais que o
 * drift NÃO explica — congestionamento, saldo reservado não devolvido,
 * blockhash expirado.
 *
 * Lança na Fase 1 de propósito. O piso de reversão vindo do drift
 * (RevertRateFloor) é mensurável agora; txFailureRate não é, e somar os
 * dois sem medir seria dobrar a mesma incerteza.
 */
export function effectiveCostWithFailures(cost: RoundTripCost): number {
  if (cost.txFailureRate == null) {
    throw new Error(
      "txFailureRate ausente: só existe após execução real (Fase 2). " +
        "Use RevertRateFloor para a parcela que o drift explica."
    );
  }
  if (cost.txFailureRate < 0 || cost.txFailureRate >= 1) {
    throw new Error(`txFailureRate fora de [0,1): ${cost.txFailureRate}`);
  }
  if (cost.totalSunkCostUsdc == null || cost.networkCostUsdc == null) {
    throw new Error("custo base incompleto: não dá para somar falhas em cima.");
  }
  const f = cost.txFailureRate;
  return cost.totalSunkCostUsdc + (f / (1 - f)) * cost.networkCostUsdc;
}

/**
 * Custo de setup do enxame inteiro (ADR-001: carteira única).
 * Usa o preço de SOL MEDIDO na amostra, nunca uma constante.
 */
export function portfolioSetup(params: {
  mints: number;
  rentLamportsPerAta: number;
  createTxFeeLamports: number | null;
  solUsdcMeasuredPrice: number | null;
}): PortfolioSetupCost {
  const { mints, rentLamportsPerAta, createTxFeeLamports, solUsdcMeasuredPrice } = params;
  const totalRentLamports = mints * rentLamportsPerAta;
  return {
    mints,
    rentLamportsPerAta,
    totalRentLamports,
    totalRentUsdc:
      solUsdcMeasuredPrice != null ? lamportsToUsdc(totalRentLamports, solUsdcMeasuredPrice) : null,
    createTxFeeLamports,
    createTxFeeUsdc:
      createTxFeeLamports != null && solUsdcMeasuredPrice != null
        ? lamportsToUsdc(createTxFeeLamports * mints, solUsdcMeasuredPrice)
        : null,
    rentRecoverable: true,
  };
}

/**
 * Guarda da Fase 2 (emenda 3): compara slippage realizado contra o cotado.
 * Dispara o kill switch quando a divergência excede o limiar.
 * Lança se chamado com uma perna que ainda não foi executada de verdade.
 */
export function slippageDivergence(leg: LegCost): number {
  if (leg.realizedSlippagePct == null) {
    throw new Error(
      "realizedSlippagePct ausente: só existe após execução real (Fase 2). " +
        "Não use quotedPriceImpactPct como substituto."
    );
  }
  if (leg.quotedPriceImpactPct == null) {
    throw new Error("quotedPriceImpactPct ausente: sem baseline para comparar.");
  }
  return leg.realizedSlippagePct - leg.quotedPriceImpactPct;
}
