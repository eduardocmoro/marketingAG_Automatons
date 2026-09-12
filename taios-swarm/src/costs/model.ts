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

  slippageBps: number | null;
  otherAmountThreshold: string | null;
  route: Route;
  compute: ComputeMeasurement;
  networkFee: NetworkFee | null;
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
  };
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
