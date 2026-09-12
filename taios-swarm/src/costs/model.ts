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
 * Rent de ATA (emenda 3).
 * O rent em si é CAPITAL TRAVADO RECUPERÁVEL — volta ao fechar a conta.
 * As taxas de tx para criar e fechar são CUSTO AFUNDADO.
 * Nunca some os dois na mesma linha.
 */
export interface AtaRentCost {
  lamportsPerAccount: number;
  /** true — o rent retorna integralmente no close. */
  recoverable: true;
  /** Afundado: taxa da tx que cria a conta. */
  createTxFeeLamports: number | null;
  /** Afundado: taxa da tx que fecha a conta. */
  closeTxFeeLamports: number | null;
  /** Quantas ATAs novas esta estratégia exige (0 se já existem). */
  newAccountsRequired: number;
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

  /** Capital travado, NÃO afundado. Reportado separado. */
  ataRentLockedUsdc: number | null;
  /** Taxas de tx de criação/fechamento de ATA. Afundado. */
  ataTxCostUsdc: number | null;

  /** swapLoss + networkCost + ataTxCost. Exclui rent recuperável. */
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
  ataRent: AtaRentCost | null;
}): RoundTripCost {
  const { phase, timestampUtc, positionSizeUsdc, solUsdcQuotedPrice, legs, startUsdc, endUsdc, ataRent } = params;

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

  let ataRentLockedUsdc: number | null = null;
  let ataTxCostUsdc: number | null = null;
  if (ataRent && solUsdcQuotedPrice != null) {
    ataRentLockedUsdc = lamportsToUsdc(
      ataRent.lamportsPerAccount * ataRent.newAccountsRequired,
      solUsdcQuotedPrice
    );
    const txLamports = (ataRent.createTxFeeLamports ?? 0) + (ataRent.closeTxFeeLamports ?? 0);
    ataTxCostUsdc = lamportsToUsdc(txLamports * ataRent.newAccountsRequired, solUsdcQuotedPrice);
  }

  const totalSunkCostUsdc =
    networkCostUsdc != null ? swapLossUsdc + networkCostUsdc + (ataTxCostUsdc ?? 0) : null;

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
    ataRentLockedUsdc,
    ataTxCostUsdc,
    totalSunkCostUsdc,
    totalSunkCostPct,
    breakEvenMovePct: totalSunkCostPct,
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
