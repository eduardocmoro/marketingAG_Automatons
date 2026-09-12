#!/usr/bin/env node
/**
 * TAIOS-Swarm — DAY 1: Medição de Custo Real de Round Trip (Solana)
 *
 * ZERO código de estratégia. Este script apenas MEDE.
 *
 * Regra inviolável: nenhum número é inventado. Todo valor vem de uma
 * chamada real (RPC Solana ou Jupiter). Quando uma medição falha, o campo
 * recebe null e um campo `*_source` / `error` registra o motivo.
 *
 * Saída: measurements/day1_costs.jsonl (append-only, 1 amostra por linha).
 * Rode N vezes ao longo de dias para acumular distribuição.
 *
 * Uso:
 *   node scripts/measure-day1.mjs
 *
 * Env opcionais:
 *   SOLANA_RPC_URL   RPC dedicado (recomendado; o público é rate-limited)
 *   MEASURE_PUBKEY   Conta pública usada SOMENTE para simulateTransaction
 *                    (leitura; nada é assinado nem enviado à rede)
 */

import { appendFileSync, mkdirSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");
const OUT_FILE = join(ROOT, "measurements", "day1_costs.jsonl");

// ── Constantes de protocolo (verificáveis on-chain) ──────────────────
const SOL_MINT = "So11111111111111111111111111111111111111112";
const USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
const SOL_DECIMALS = 9;
const USDC_DECIMALS = 6;
const LAMPORTS_PER_SOL = 1_000_000_000;
const BASE_FEE_LAMPORTS_PER_SIG = 5_000; // fixo no protocolo Solana
const SPL_TOKEN_ACCOUNT_BYTES = 165; // tamanho de uma ATA SPL

// Emenda 1: núcleo da tese (US$0,50–5) + escala comparativa (10–500)
const TRADE_SIZES_USDC = [0.5, 1, 2, 5, 10, 50, 100, 500];

// Horizontes de latência para medir envelhecimento de cotação.
// Slot time da Solana é ~350ms — uma quote envelhece dentro do loop.
const LATENCY_HORIZONS_MS = [250, 500, 1000, 2000, 5000];

// Sonda de referência: notional pequeno o bastante para o price impact ser
// desprezível, cotado em par com cada quote de tamanho real. NÃO é um mid
// de oráculo — é uma quote executável de notional mínimo, e o nome reflete
// isso. Serve para separar movimento de preço de efeito de tamanho.
const MID_PROBE_USDC = 0.1;

// Drift so no nucleo da tese: e movimento de PRECO, quase independente do
// tamanho, e os tamanhos grandes abriam split de rota e eram excluidos da
// calibracao de qualquer forma. Corta 4 tamanhos x 12 chamadas por execucao.
const DRIFT_SIZES_USDC = [0.5, 1, 2, 5];

const RPC_URL = process.env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com";

/**
 * RPC dedicado carrega a API key na URL. Ela NUNCA pode ir para stdout nem
 * para o jsonl — o jsonl é commitado como trilha de auditoria.
 */
function maskUrl(u) {
  try {
    const url = new URL(u);
    const tail = u.slice(-4);
    return `${url.protocol}//${url.host}/…${tail}`;
  } catch {
    return "<url invalida>";
  }
}
const RPC_URL_MASKED = maskUrl(RPC_URL);

// Throttle global da Jupiter. O free tier de lite-api derruba com 429 e
// dado parcial silencioso é pior que menos dado.
const JUP_MIN_GAP_MS = Number(process.env.JUP_MIN_GAP_MS || 1200);
let lastJupCallAt = 0;
const rateLimitEvents = [];

async function throttleJup(minGapMs = JUP_MIN_GAP_MS) {
  const wait = lastJupCallAt + minGapMs - Date.now();
  if (wait > 0) await sleep(wait);
  lastJupCallAt = Date.now();
}

/** fetch com backoff exponencial em 429. Toda ocorrência fica registrada. */
async function fetchWithBackoff(url, opts, label, retries = 4) {
  let lastStatus = null;
  for (let attempt = 0; attempt <= retries; attempt++) {
    const res = await fetch(url, opts);
    if (res.status !== 429) return res;
    lastStatus = 429;
    const backoffMs = 1000 * 2 ** attempt;
    rateLimitEvents.push({
      label,
      attempt,
      backoffMs,
      atUtc: new Date().toISOString(),
    });
    if (attempt < retries) await sleep(backoffMs);
  }
  throw new Error(`HTTP ${lastStatus} após ${retries + 1} tentativas (${label})`);
}

// Conta pública apenas para montar/simular a tx e obter unitsConsumed.
// Precisa ter saldo de SOL e USDC para a simulação não abortar por fundos.
const MEASURE_PUBKEY =
  process.env.MEASURE_PUBKEY || "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9";

// Jupiter migrou de host ao longo do tempo; tenta em ordem.
const JUP_HOSTS = [
  "https://lite-api.jup.ag/swap/v1",
  "https://quote-api.jup.ag/v6",
  "https://api.jup.ag/swap/v1",
];

const TIMEOUT_MS = 20_000;

// ── Helpers ──────────────────────────────────────────────────────────

const toRaw = (amount, decimals) =>
  BigInt(Math.round(amount * 10 ** decimals)).toString();

const fromRaw = (raw, decimals) => Number(raw) / 10 ** decimals;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function percentile(sorted, p) {
  if (sorted.length === 0) return null;
  if (sorted.length === 1) return sorted[0];
  const idx = (sorted.length - 1) * p;
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return sorted[lo];
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
}

async function rpc(method, params) {
  const res = await fetch(RPC_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`RPC ${method} HTTP ${res.status}`);
  const json = await res.json();
  if (json.error) throw new Error(`RPC ${method}: ${json.error.message}`);
  return json.result;
}

/** Decodifica o compact-u16 inicial da tx serializada = nº de assinaturas. */
function countSignatures(base64Tx) {
  const buf = Buffer.from(base64Tx, "base64");
  let value = 0;
  let shift = 0;
  let i = 0;
  for (;;) {
    const byte = buf[i++];
    value |= (byte & 0x7f) << shift;
    if ((byte & 0x80) === 0) break;
    shift += 7;
    if (shift > 21) throw new Error("compact-u16 malformado");
  }
  return value;
}

// ── Jupiter ──────────────────────────────────────────────────────────

let resolvedJupHost = null;
// Preco de SOL medido nesta execucao, usado para converter taxas de pool
// denominadas em SOL para USDC na validacao cruzada.
let refSolUsdcPrice = null;

async function jupQuote(inputMint, outputMint, amountRaw, slippageBps = 50, minGapMs) {
  const hosts = resolvedJupHost ? [resolvedJupHost] : JUP_HOSTS;
  let lastErr;
  for (const host of hosts) {
    const url =
      `${host}/quote?inputMint=${inputMint}&outputMint=${outputMint}` +
      `&amount=${amountRaw}&slippageBps=${slippageBps}&onlyDirectRoutes=false`;
    try {
      await throttleJup(minGapMs);
      const res = await fetchWithBackoff(url, {
        headers: { Accept: "application/json" },
        signal: AbortSignal.timeout(TIMEOUT_MS),
      }, `quote ${amountRaw}`);
      if (!res.ok) {
        lastErr = new Error(`${host} HTTP ${res.status}: ${(await res.text()).slice(0, 160)}`);
        continue;
      }
      const json = await res.json();
      if (!json.outAmount) {
        lastErr = new Error(`${host}: resposta sem outAmount`);
        continue;
      }
      resolvedJupHost = host;
      return json;
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr || new Error("Todos os hosts Jupiter falharam");
}

async function jupSwapTx(quoteResponse) {
  const host = resolvedJupHost || JUP_HOSTS[0];
  await throttleJup();
  const res = await fetchWithBackoff(`${host}/swap`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      quoteResponse,
      userPublicKey: MEASURE_PUBKEY,
      wrapAndUnwrapSol: true,
      dynamicComputeUnitLimit: true,
    }),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  }, "swap");
  if (!res.ok) {
    throw new Error(`swap HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
  }
  return res.json();
}

/**
 * Assinatura de rota: pools concretos e split. Duas quotes com a mesma
 * assinatura passaram pela mesma liquidez.
 *
 * Jupiter roteia POR TAMANHO: uma sonda de US$0,10 pode sair single-hop
 * enquanto US$500 abre split multi-venue. Sem comparar isso, a diferença
 * entre os dois drifts mistura efeito de ROTA com efeito de TAMANHO.
 */
function routeSignature(quote) {
  const plan = quote?.routePlan || [];
  if (plan.length === 0) return null;
  return plan
    .map((s) => `${s.swapInfo?.ammKey ?? "?"}@${s.percent ?? "?"}`)
    .sort()
    .join("|");
}

function routeLabel(quote) {
  const plan = quote?.routePlan || [];
  if (plan.length === 0) return null;
  return `[${plan.length}]` + plan.map((s) => s.swapInfo?.label ?? "?").join("+");
}

/**
 * Soma feeAmount do routePlan por mint.
 *
 * Caminho INDEPENDENTE do encadeamento de quotes: é o que o roteador diz
 * ter cobrado de taxa de pool. Se a perda observada no round trip não bater
 * com esta soma, o bug está no nosso cálculo, não no roteador.
 */
function routeFeesByMint(quote) {
  const byMint = {};
  for (const step of quote?.routePlan || []) {
    const si = step.swapInfo || {};
    if (si.feeAmount != null && si.feeMint) {
      byMint[si.feeMint] = (BigInt(byMint[si.feeMint] ?? "0") + BigInt(si.feeAmount)).toString();
    }
  }
  return byMint;
}

/** Converte o dicionário de taxas para USDC usando o preço medido de SOL. */
function feesToUsdc(byMint, solUsdcPrice) {
  let usdc = 0;
  let unconverted = null;
  for (const [mint, amount] of Object.entries(byMint)) {
    if (mint === USDC_MINT) {
      usdc += fromRaw(amount, USDC_DECIMALS);
    } else if (mint === SOL_MINT) {
      if (solUsdcPrice == null) {
        unconverted = (unconverted || 0) + Number(amount);
        continue;
      }
      usdc += fromRaw(amount, SOL_DECIMALS) * solUsdcPrice;
    } else {
      // Mint intermediário numa rota multi-hop: sem preço, não converte.
      unconverted = (unconverted || 0) + Number(amount);
    }
  }
  return { usdc, unconvertedRawUnits: unconverted };
}

/**
 * Taxa efetiva do pool NAQUELE salto, direto do routePlan.
 *
 * A taxa e cobrada em feeMint, que pode ser o mint de entrada ou o de saida
 * do salto — a base da divisao muda conforme o caso. Feito certo, o numero
 * que sai e o TIER do pool: 25 bps = Raydium classico, 1-5 bps = CLMM barato.
 *
 * Isto e RESULTADO do Day 1, nao premissa herdada. Nada de assumir 0,25%.
 */
function hopFeeRateBps(s) {
  if (s.feeAmount == null || !s.feeMint) return null;
  const fee = Number(s.feeAmount);
  if (!Number.isFinite(fee) || fee < 0) return null;
  let base = null;
  if (s.feeMint === s.inputMint) base = Number(s.inAmount);
  else if (s.feeMint === s.outputMint) base = Number(s.outAmount);
  if (base == null || !Number.isFinite(base) || base <= 0) return null;
  return (fee / base) * 10_000;
}

/**
 * Emenda 5: lê o routePlan real devolvido pelo roteador.
 * Nada de fixar 0,25% do Raydium — registra venue/fee/tier por salto.
 */
function extractRoute(quote) {
  const plan = quote.routePlan || [];
  return {
    hops: plan.length,
    // Assinatura: permite agrupar tamanhos que atravessaram a MESMA
    // liquidez. Comparar perda entre rotas diferentes mistura taxa de
    // venue com impacto de tamanho e nao testa nada.
    signature: routeSignature(quote),
    legs: plan.map((step) => {
      const s = step.swapInfo || {};
      return {
        venue: s.label ?? null,
        ammKey: s.ammKey ?? null,
        inputMint: s.inputMint ?? null,
        outputMint: s.outputMint ?? null,
        inAmount: s.inAmount ?? null,
        outAmount: s.outAmount ?? null,
        feeAmount: s.feeAmount ?? null,
        feeMint: s.feeMint ?? null,
        feeRateBps: hopFeeRateBps(s),
        percent: step.percent ?? null,
      };
    }),
  };
}

// ── Emenda 2: custo de rede real ─────────────────────────────────────

/**
 * cu_consumed via simulateTransaction (unitsConsumed).
 * Fallback declarado: estimativa do próprio Jupiter. Nunca um chute.
 */
async function measureComputeUnits(swapResponse) {
  const tx = swapResponse.swapTransaction;
  let signatures = null;
  try {
    signatures = countSignatures(tx);
  } catch (e) {
    signatures = null;
  }

  try {
    const sim = await rpc("simulateTransaction", [
      tx,
      {
        encoding: "base64",
        replaceRecentBlockhash: true,
        sigVerify: false,
        commitment: "confirmed",
      },
    ]);
    const units = sim?.value?.unitsConsumed ?? null;
    if (units != null && units > 0) {
      return {
        cuConsumed: units,
        cuConsumedSource: "simulation",
        signatures,
        simError: sim.value.err ? JSON.stringify(sim.value.err).slice(0, 200) : null,
      };
    }
    return {
      cuConsumed: swapResponse.computeUnitLimit ?? null,
      cuConsumedSource:
        swapResponse.computeUnitLimit != null ? "jupiter_estimate" : "unavailable",
      signatures,
      simError: sim?.value?.err ? JSON.stringify(sim.value.err).slice(0, 200) : "unitsConsumed ausente",
    };
  } catch (e) {
    return {
      cuConsumed: swapResponse.computeUnitLimit ?? null,
      cuConsumedSource:
        swapResponse.computeUnitLimit != null ? "jupiter_estimate" : "unavailable",
      signatures,
      simError: e.message.slice(0, 200),
    };
  }
}

/**
 * Emenda 2: priority fee amostrado como distribuição, não ponto único.
 * getRecentPrioritizationFees devolve ~150 slots recentes.
 */
async function measurePriorityFeeDistribution(poolAccounts = []) {
  const out = { global: null, solUsdc: null, error: null };
  try {
    const global = await rpc("getRecentPrioritizationFees", [[]]);
    const fees = global.map((f) => f.prioritizationFee).sort((a, b) => a - b);
    out.global = {
      samples: fees.length,
      median: percentile(fees, 0.5),
      p25: percentile(fees, 0.25),
      p50: percentile(fees, 0.5),
      p75: percentile(fees, 0.75),
      p90: percentile(fees, 0.9),
      p99: percentile(fees, 0.99),
      min: fees[0] ?? null,
      max: fees[fees.length - 1] ?? null,
    };
  } catch (e) {
    out.error = e.message.slice(0, 200);
  }
  // Mints NAO sao as contas que o swap escreve. As contas relevantes sao os
  // POOLS (ammKey do routePlan), que a tx trava para escrita. Sem esse
  // filtro o RPC devolve ruido da rede inteira.
  if (poolAccounts.length === 0) {
    out.error = (out.error ? out.error + " | " : "") + "sem contas de pool para filtrar";
    return out;
  }
  out.filterAccounts = poolAccounts;
  try {
    const scoped = await rpc("getRecentPrioritizationFees", [poolAccounts.slice(0, 128)]);
    const fees = scoped.map((f) => f.prioritizationFee).sort((a, b) => a - b);
    // Dispersao importa mais que o centro: se a cauda persistir DEPOIS do
    // filtro por ammKey, o custo em US$1 varia de forma imprevisivel e
    // nenhum alvo fixo de lucro por trade se sustenta.
    out.solUsdc = {
      samples: fees.length,
      median: percentile(fees, 0.5),
      p25: percentile(fees, 0.25),
      p50: percentile(fees, 0.5),
      p75: percentile(fees, 0.75),
      p90: percentile(fees, 0.9),
      p99: percentile(fees, 0.99),
      min: fees[0] ?? null,
      max: fees[fees.length - 1] ?? null,
    };
  } catch (e) {
    out.error = (out.error ? out.error + " | " : "") + e.message.slice(0, 200);
  }
  return out;
}

function networkFeeLamports(signatures, cuPriceMicroLamports, cuConsumed) {
  if (signatures == null || cuPriceMicroLamports == null || cuConsumed == null) return null;
  const base = BASE_FEE_LAMPORTS_PER_SIG * signatures;
  const priority = Math.ceil((cuPriceMicroLamports * cuConsumed) / 1_000_000);
  return { base, priority, total: base + priority };
}

// ── Emenda 3: ATA rent ───────────────────────────────────────────────

async function measureAtaRent() {
  // Fórmula de rent da Solana:
  //   (ACCOUNT_STORAGE_OVERHEAD + bytes) x LAMPORTS_PER_BYTE_YEAR x THRESHOLD
  //   = (128 + bytes) x 3480 x 2
  // Sonda vários tamanhos para diagnosticar divergência entre medido e
  // fórmula em vez de assumir a constante.
  const predict = (bytes) => (128 + bytes) * 3480 * 2;
  const probes = [];
  for (const bytes of [0, 82, SPL_TOKEN_ACCOUNT_BYTES]) {
    try {
      const measured = await rpc("getMinimumBalanceForRentExemption", [bytes]);
      probes.push({
        accountBytes: bytes,
        measuredLamports: measured,
        formulaLamports: predict(bytes),
        matchesFormula: measured === predict(bytes),
        error: null,
      });
    } catch (e) {
      probes.push({
        accountBytes: bytes,
        measuredLamports: null,
        formulaLamports: predict(bytes),
        matchesFormula: null,
        error: e.message.slice(0, 200),
      });
    }
  }
  const ata = probes.find((p) => p.accountBytes === SPL_TOKEN_ACCOUNT_BYTES);
  return {
    // Valor MEDIDO manda. A fórmula fica registrada ao lado para o
    // relatório poder apontar a divergência em vez de escondê-la.
    lamportsPerAccount: ata?.measuredLamports ?? null,
    accountBytes: SPL_TOKEN_ACCOUNT_BYTES,
    formulaLamports: predict(SPL_TOKEN_ACCOUNT_BYTES),
    matchesFormula: ata?.matchesFormula ?? null,
    probes,
    recoverable: true,
    note: "capital travado recuperável — NÃO é custo afundado",
    error: ata?.error ?? null,
  };
}

// ── Drift de cotação por latência ────────────────────────────────────

/**
 * Custo do TEMPO, não do tamanho.
 *
 * Cota em t0, espera cada horizonte e recota o MESMO par e tamanho.
 * O delta de outAmount é um piso empírico de slippage — quanto a cotação
 * envelhece enquanto o loop decide — sem executar nada.
 *
 * Distinto de quotedPriceImpact (função do tamanho) e de realizedSlippage
 * (só existe com execução real, Fase 2).
 *
 * Sinal: driftBps > 0 = receberia MAIS (a favor). < 0 = MENOS (contra).
 * A distribuição é aproximadamente simétrica; o que custa é a magnitude.
 */
async function measureQuoteDrift(sizeUsdc) {
  const inRaw = toRaw(sizeUsdc, USDC_DECIMALS);
  const probeRaw = toRaw(MID_PROBE_USDC, USDC_DECIMALS);
  const result = {
    positionSizeUsdc: sizeUsdc,
    midProbeUsdc: MID_PROBE_USDC,
    baseOutAmount: null,
    baseProbeOutAmount: null,
    baseQuoteAtUtc: null,
    points: [],
    error: null,
  };

  let base;
  let baseProbe = null;
  let baseSizeSig = null;
  let baseProbeSig = null;
  let baseDoneAt;
  try {
    const t0 = Date.now();
    const q0 = await jupQuote(USDC_MINT, SOL_MINT, inRaw, 50, 0);
    base = Number(q0.outAmount);
    baseSizeSig = routeSignature(q0);
    result.baseOutAmount = q0.outAmount;
    result.baseRoute = routeLabel(q0);
    result.baseQuoteAtUtc = new Date(t0).toISOString();
    // Sonda pareada, logo em seguida — a poucos ms da quote de tamanho.
    try {
      const p0 = await jupQuote(USDC_MINT, SOL_MINT, probeRaw, 50, 0);
      const probeHops = (p0.routePlan || []).length;
      // Sonda degenerada (sem rota ou sem saída) não serve de referência.
      if (probeHops > 0 && Number(p0.outAmount) > 0) {
        baseProbe = Number(p0.outAmount);
        baseProbeSig = routeSignature(p0);
        result.baseProbeOutAmount = p0.outAmount;
        result.baseProbeRoute = routeLabel(p0);
      } else {
        result.probeDegenerate = true;
      }
    } catch {
      baseProbe = null;
    }
    baseDoneAt = Date.now();
  } catch (e) {
    result.error = e.message.slice(0, 200);
    return result;
  }

  for (const horizon of LATENCY_HORIZONS_MS) {
    const waitMs = baseDoneAt + horizon - Date.now();
    if (waitMs > 0) await sleep(waitMs);

    // O horizonte real fica entre o envio e a resposta — registra os dois
    // em vez de assumir que a espera nominal foi exata.
    const sentAt = Date.now();
    try {
      const q = await jupQuote(USDC_MINT, SOL_MINT, inRaw, 50, 0);
      const recvAt = Date.now();
      const out = Number(q.outAmount);

      // Sonda pareada: mesmo instante, notional desprezível.
      let midDriftBps = null;
      let probeOut = null;
      let probeSig = null;
      let probeRouteStr = null;
      if (baseProbe != null) {
        try {
          const p = await jupQuote(USDC_MINT, SOL_MINT, probeRaw, 50, 0);
          if ((p.routePlan || []).length > 0 && Number(p.outAmount) > 0) {
            probeOut = p.outAmount;
            probeSig = routeSignature(p);
            probeRouteStr = routeLabel(p);
            midDriftBps = ((Number(p.outAmount) - baseProbe) / baseProbe) * 10_000;
          }
        } catch {
          midDriftBps = null;
        }
      }

      const sizeSig = routeSignature(q);
      // sizeComponent só é atribuível ao TAMANHO quando as duas quotes
      // passaram pela MESMA liquidez. Rotas diferentes contaminam a conta.
      const routeMatch = sizeSig != null && probeSig != null ? sizeSig === probeSig : null;
      const driftBps = ((out - base) / base) * 10_000;

      result.points.push({
        nominalHorizonMs: horizon,
        elapsedMsAtRequest: sentAt - baseDoneAt,
        elapsedMsAtResponse: recvAt - baseDoneAt,
        outAmount: q.outAmount,
        driftBps,
        probeOutAmount: probeOut,
        midDriftBps,
        sizeComponentBps: midDriftBps != null ? driftBps - midDriftBps : null,
        routeMatch,
        sizeRoute: routeLabel(q),
        probeRoute: probeRouteStr,
        // Rota que muda entre t0 e t0+horizonte mete descontinuidade de
        // roteamento dentro do "drift" — não é só movimento de preço.
        sizeRouteChangedFromBase: baseSizeSig != null && sizeSig != null ? sizeSig !== baseSizeSig : null,
        probeRouteChangedFromBase:
          baseProbeSig != null && probeSig != null ? probeSig !== baseProbeSig : null,
        error: null,
      });
    } catch (e) {
      result.points.push({
        nominalHorizonMs: horizon,
        elapsedMsAtRequest: sentAt - baseDoneAt,
        elapsedMsAtResponse: null,
        outAmount: null,
        driftBps: null,
        probeOutAmount: null,
        midDriftBps: null,
        sizeComponentBps: null,
        routeMatch: null,
        sizeRoute: null,
        probeRoute: null,
        sizeRouteChangedFromBase: null,
        probeRouteChangedFromBase: null,
        error: e.message.slice(0, 150),
      });
    }
  }

  return result;
}

// ── Emenda 4: round trip nas duas direções ───────────────────────────

/**
 * Perna 1: N USDC -> SOL.  Perna 2: exatamente o outAmount da perna 1 -> USDC.
 * A perda do round trip (N - M) captura fee de pool + price impact das
 * DUAS direções, que são assimétricas em CLMM.
 */
async function measureRoundTrip(sizeUsdc, priorityFee) {
  const sample = {
    positionSizeUsdc: sizeUsdc,
    leg1: null,
    leg2: null,
    startUsdc: sizeUsdc,
    endUsdc: null,
    roundTripReturnPct: null,   // <0 = perda, >0 = ganho
    swapLossUsdc: null,
    swapLossPct: null,          // = -roundTripReturnPct; positivo E perda
    legGapMs: null,             // tempo entre as duas quotes
    leg1QuoteMs: null,
    feeCrossCheck: null,
    error: null,
  };

  try {
    const inRaw = toRaw(sizeUsdc, USDC_DECIMALS);

    // As DUAS quotes primeiro, coladas no tempo. Construir a tx de swap e
    // simular entre elas metia 0,5–2s de deriva de preço DENTRO do round
    // trip, contaminando a perda com movimento de mercado.
    const t1 = Date.now();
    const q1 = await jupQuote(USDC_MINT, SOL_MINT, inRaw);
    const t1done = Date.now();
    const q2 = await jupQuote(SOL_MINT, USDC_MINT, q1.outAmount);
    const t2done = Date.now();
    sample.legGapMs = t2done - t1done;
    sample.leg1QuoteMs = t1done - t1;

    // CU medido depois: o timing dele não afeta mais o encadeamento.
    let swap1 = null;
    let cu1 = { cuConsumed: null, cuConsumedSource: "unavailable", signatures: null, simError: null };
    try {
      swap1 = await jupSwapTx(q1);
      cu1 = await measureComputeUnits(swap1);
    } catch (e) {
      cu1.simError = e.message.slice(0, 200);
    }

    sample.leg1 = {
      direction: "USDC->SOL",
      inAmountRaw: q1.inAmount,
      outAmountRaw: q1.outAmount,
      // Emenda 3: isto é COTADO, não realizado. realizedSlippagePct só na Fase 2.
      quotedPriceImpactPct: q1.priceImpactPct != null ? Number(q1.priceImpactPct) : null,
      realizedSlippagePct: null,
      slippageBps: q1.slippageBps ?? null,
      otherAmountThreshold: q1.otherAmountThreshold ?? null,
      route: extractRoute(q1),
      routeFeesByMint: routeFeesByMint(q1),
      compute: cu1,
      jupiterSuggestedPriorityLamports: swap1?.prioritizationFeeLamports ?? null,
    };

    let swap2 = null;
    let cu2 = { cuConsumed: null, cuConsumedSource: "unavailable", signatures: null, simError: null };
    try {
      swap2 = await jupSwapTx(q2);
      cu2 = await measureComputeUnits(swap2);
    } catch (e) {
      cu2.simError = e.message.slice(0, 200);
    }

    sample.leg2 = {
      direction: "SOL->USDC",
      inAmountRaw: q2.inAmount,
      outAmountRaw: q2.outAmount,
      quotedPriceImpactPct: q2.priceImpactPct != null ? Number(q2.priceImpactPct) : null,
      realizedSlippagePct: null,
      slippageBps: q2.slippageBps ?? null,
      otherAmountThreshold: q2.otherAmountThreshold ?? null,
      route: extractRoute(q2),
      routeFeesByMint: routeFeesByMint(q2),
      compute: cu2,
      jupiterSuggestedPriorityLamports: swap2?.prioritizationFeeLamports ?? null,
    };

    sample.endUsdc = fromRaw(q2.outAmount, USDC_DECIMALS);

    // CONVENÇÃO DE SINAL, explícita:
    //   roundTripReturnPct < 0  -> terminou com MENOS USDC (perda)
    //   roundTripReturnPct > 0  -> terminou com MAIS USDC (ganho)
    //   swapLossPct = -roundTripReturnPct, então positivo É perda.
    sample.roundTripReturnPct = (sample.endUsdc / sample.startUsdc - 1) * 100;
    sample.swapLossUsdc = sample.startUsdc - sample.endUsdc;
    sample.swapLossPct = -sample.roundTripReturnPct;

    // ── Validação cruzada independente ──
    // A soma de feeAmount das duas pernas tem que explicar a perda
    // observada. Se não bater, o bug está no encadeamento, não no roteador.
    const f1 = feesToUsdc(sample.leg1.routeFeesByMint, refSolUsdcPrice);
    const f2 = feesToUsdc(sample.leg2.routeFeesByMint, refSolUsdcPrice);
    const feeUsdcTotal = f1.usdc + f2.usdc;
    sample.feeCrossCheck = {
      leg1FeeUsdc: f1.usdc,
      leg2FeeUsdc: f2.usdc,
      totalFeeUsdc: feeUsdcTotal,
      totalFeePctOfPosition: (feeUsdcTotal / sample.startUsdc) * 100,
      observedLossUsdc: sample.swapLossUsdc,
      // observado - esperado. Deve ficar perto de zero mais price impact.
      discrepancyUsdc: sample.swapLossUsdc - feeUsdcTotal,
      unconvertedLeg1: f1.unconvertedRawUnits,
      unconvertedLeg2: f2.unconvertedRawUnits,
      solUsdcPriceUsed: refSolUsdcPrice,
    };

    // Custo de rede das duas pernas, nos cenários mediana e p90.
    const cuPriceMedian = priorityFee.solUsdc?.median ?? priorityFee.global?.median ?? null;
    const cuPriceP90 = priorityFee.solUsdc?.p90 ?? priorityFee.global?.p90 ?? null;

    sample.networkFee = {
      cuPriceMicroLamportsMedian: cuPriceMedian,
      cuPriceMicroLamportsP90: cuPriceP90,
      leg1Median: networkFeeLamports(cu1.signatures, cuPriceMedian, cu1.cuConsumed),
      leg2Median: networkFeeLamports(cu2.signatures, cuPriceMedian, cu2.cuConsumed),
      leg1P90: networkFeeLamports(cu1.signatures, cuPriceP90, cu1.cuConsumed),
      leg2P90: networkFeeLamports(cu2.signatures, cuPriceP90, cu2.cuConsumed),
    };
  } catch (e) {
    sample.error = e.message.slice(0, 300);
  }

  return sample;
}

// ── Main ─────────────────────────────────────────────────────────────

async function main() {
  const startedAt = new Date().toISOString();
  console.log("TAIOS-Swarm — Day 1: medição de custo de round trip");
  console.log(`RPC      : ${RPC_URL_MASKED}`);
  console.log(`Pubkey   : ${MEASURE_PUBKEY} (somente simulação, nada assinado)`);
  console.log(`Tamanhos : ${TRADE_SIZES_USDC.map((s) => "$" + s).join(", ")}`);
  console.log(`Drift em : ${DRIFT_SIZES_USDC.map((s) => "$" + s).join(", ")}`);
  console.log(`Throttle : ${JUP_MIN_GAP_MS}ms entre chamadas Jupiter`);
  console.log("");

  // [1] Preço primeiro: a validação cruzada de taxas precisa dele para
  // converter fees denominadas em SOL.
  console.log("[1/6] Cotando preço de referência SOL/USDC (1 SOL)...");
  let priceError = null;
  try {
    const refQuote = await jupQuote(SOL_MINT, USDC_MINT, toRaw(1, SOL_DECIMALS));
    refSolUsdcPrice = fromRaw(refQuote.outAmount, USDC_DECIMALS);
    console.log(`      1 SOL = ${refSolUsdcPrice.toFixed(4)} USDC (cotação, não mid oracle)`);
  } catch (e) {
    priceError = e.message.slice(0, 200);
    console.log(`      FALHOU: ${priceError}`);
  }

  // [2] Contas de pool que o swap efetivamente escreve — sem isso o
  // getRecentPrioritizationFees devolve ruído da rede inteira.
  console.log("[2/6] Descobrindo contas de pool da rota SOL/USDC...");
  let poolAccounts = [];
  try {
    const probe = await jupQuote(USDC_MINT, SOL_MINT, toRaw(1, USDC_DECIMALS));
    poolAccounts = [
      ...new Set((probe.routePlan || []).map((r) => r.swapInfo?.ammKey).filter(Boolean)),
    ];
    console.log(`      ${poolAccounts.length} pool(s): ${poolAccounts.join(", ") || "nenhum"}`);
  } catch (e) {
    console.log(`      FALHOU: ${e.message.slice(0, 160)}`);
  }

  console.log("[3/6] Amostrando priority fee (global e filtrado por pool)...");
  const priorityFee = await measurePriorityFeeDistribution(poolAccounts);
  if (priorityFee.global) {
    const g = priorityFee.global;
    console.log(`      global : ${g.samples} slots | mediana ${g.median} | p90 ${g.p90}`);
  }
  if (priorityFee.solUsdc) {
    const f = priorityFee.solUsdc;
    console.log(`      pools  : ${f.samples} slots | mediana ${f.median} | p90 ${f.p90}`);
  } else {
    console.log(`      pools  : indisponível (${priorityFee.error || "sem filtro"})`);
  }

  console.log("[4/6] Medindo rent (sondando vários tamanhos de conta)...");
  const ataRent = await measureAtaRent();
  for (const pr of ataRent.probes || []) {
    const flag = pr.matchesFormula === false ? "  <-- DIVERGE DA FORMULA" : "";
    console.log(
      `      ${String(pr.accountBytes).padStart(3)} bytes: medido ${pr.measuredLamports ?? "erro"}` +
        ` | formula ${pr.formulaLamports}${flag}`
    );
  }

  console.log("[5/6] Medindo round trip por tamanho...");
  const roundTrips = [];
  for (const size of TRADE_SIZES_USDC) {
    process.stdout.write(`      $${size} ... `);
    const rt = await measureRoundTrip(size, priorityFee);
    roundTrips.push(rt);
    if (rt.error) {
      console.log(`ERRO: ${rt.error}`);
    } else {
      const cc = rt.feeCrossCheck;
      console.log(
        `retorno ${rt.roundTripReturnPct >= 0 ? "+" : ""}${rt.roundTripReturnPct.toFixed(4)}%` +
          ` | taxa routePlan ${cc ? cc.totalFeePctOfPosition.toFixed(4) + "%" : "n/d"}` +
          ` | gap ${rt.legGapMs}ms`
      );
    }
  }

  console.log("[6/6] Medindo drift de cotação por latência...");
  console.log(`      horizontes: ${LATENCY_HORIZONS_MS.join("ms, ")}ms`);
  const quoteDrift = [];
  for (const size of DRIFT_SIZES_USDC) {
    process.stdout.write(`      $${size} ... `);
    const d = await measureQuoteDrift(size);
    quoteDrift.push(d);
    if (d.error) {
      console.log(`ERRO: ${d.error}`);
    } else {
      console.log(
        d.points
          .map((pt) =>
            pt.driftBps != null
              ? `${pt.nominalHorizonMs}ms:${pt.driftBps >= 0 ? "+" : ""}${pt.driftBps.toFixed(1)}`
              : `${pt.nominalHorizonMs}ms:erro`
          )
          .join(" ")
      );
    }
    // Folga entre tamanhos: a rajada de drift roda sem throttle para não
    // destruir os horizontes, então a média volta ao limite aqui.
    await sleep(JUP_MIN_GAP_MS * 6);
  }

  // Completude explícita: dado parcial nunca sai silencioso.
  const rtOk = roundTrips.filter((r) => !r.error).length;
  const driftOk = quoteDrift.filter((d) => !d.error && d.points.some((p) => p.driftBps != null)).length;
  const completeness = {
    roundTripsRequested: TRADE_SIZES_USDC.length,
    roundTripsOk: rtOk,
    driftSizesRequested: DRIFT_SIZES_USDC.length,
    driftSizesOk: driftOk,
    rateLimitHits: rateLimitEvents.length,
    complete: rtOk === TRADE_SIZES_USDC.length && driftOk === DRIFT_SIZES_USDC.length,
  };

  const record = {
    schemaVersion: 2,
    timestampUtc: startedAt,
    finishedUtc: new Date().toISOString(),
    rpcUrl: RPC_URL_MASKED, // mascarado: o jsonl é commitado
    jupiterHost: resolvedJupHost,
    measurePubkey: MEASURE_PUBKEY,
    solUsdcQuotedPrice: refSolUsdcPrice,
    solUsdcQuotedPriceError: priceError,
    baseFeeLamportsPerSignature: BASE_FEE_LAMPORTS_PER_SIG,
    poolAccounts,
    priorityFee,
    ataRent,
    roundTrips,
    latencyHorizonsMs: LATENCY_HORIZONS_MS,
    driftSizesUsdc: DRIFT_SIZES_USDC,
    quoteDrift,
    rateLimitEvents,
    completeness,
  };

  mkdirSync(dirname(OUT_FILE), { recursive: true });
  appendFileSync(OUT_FILE, JSON.stringify(record) + "\n", "utf8");

  console.log("");
  if (!completeness.complete) {
    console.log("!".repeat(70));
    console.log("AMOSTRA INCOMPLETA — nao use para decidir nada.");
    console.log(`  round trips : ${rtOk}/${TRADE_SIZES_USDC.length}`);
    console.log(`  drift       : ${driftOk}/${DRIFT_SIZES_USDC.length}`);
    console.log(`  429 da Jupiter: ${rateLimitEvents.length}`);
    if (rateLimitEvents.length > 0) {
      console.log("  Aumente o espacamento: JUP_MIN_GAP_MS=2000 bash scripts/run-day1.sh");
    }
    console.log("!".repeat(70));
  }
  console.log(`Amostra gravada em ${OUT_FILE}`);
  console.log("Analise: python3 analysis/day1_report.py");
}

main().catch((e) => {
  console.error("ERRO FATAL:", e.message);
  process.exit(1);
});
