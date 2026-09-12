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

const RPC_URL = process.env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com";

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

async function jupQuote(inputMint, outputMint, amountRaw, slippageBps = 50) {
  const hosts = resolvedJupHost ? [resolvedJupHost] : JUP_HOSTS;
  let lastErr;
  for (const host of hosts) {
    const url =
      `${host}/quote?inputMint=${inputMint}&outputMint=${outputMint}` +
      `&amount=${amountRaw}&slippageBps=${slippageBps}&onlyDirectRoutes=false`;
    try {
      const res = await fetch(url, {
        headers: { Accept: "application/json" },
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
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
  const res = await fetch(`${host}/swap`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      quoteResponse,
      userPublicKey: MEASURE_PUBKEY,
      wrapAndUnwrapSol: true,
      dynamicComputeUnitLimit: true,
    }),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (!res.ok) {
    throw new Error(`swap HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
  }
  return res.json();
}

/**
 * Emenda 5: lê o routePlan real devolvido pelo roteador.
 * Nada de fixar 0,25% do Raydium — registra venue/fee por salto.
 */
function extractRoute(quote) {
  const plan = quote.routePlan || [];
  return {
    hops: plan.length,
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
async function measurePriorityFeeDistribution() {
  const out = { global: null, solUsdc: null, error: null };
  try {
    const global = await rpc("getRecentPrioritizationFees", [[]]);
    const fees = global.map((f) => f.prioritizationFee).sort((a, b) => a - b);
    out.global = {
      samples: fees.length,
      median: percentile(fees, 0.5),
      p90: percentile(fees, 0.9),
      min: fees[0] ?? null,
      max: fees[fees.length - 1] ?? null,
    };
  } catch (e) {
    out.error = e.message.slice(0, 200);
  }
  try {
    const scoped = await rpc("getRecentPrioritizationFees", [[SOL_MINT, USDC_MINT]]);
    const fees = scoped.map((f) => f.prioritizationFee).sort((a, b) => a - b);
    out.solUsdc = {
      samples: fees.length,
      median: percentile(fees, 0.5),
      p90: percentile(fees, 0.9),
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
  try {
    const lamports = await rpc("getMinimumBalanceForRentExemption", [
      SPL_TOKEN_ACCOUNT_BYTES,
    ]);
    return {
      lamportsPerAccount: lamports,
      accountBytes: SPL_TOKEN_ACCOUNT_BYTES,
      recoverable: true, // devolvido ao fechar a conta
      note: "capital travado recuperável — NÃO é custo afundado",
      error: null,
    };
  } catch (e) {
    return { lamportsPerAccount: null, recoverable: true, error: e.message.slice(0, 200) };
  }
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
    swapLossUsdc: null,
    swapLossPct: null,
    error: null,
  };

  try {
    const inRaw = toRaw(sizeUsdc, USDC_DECIMALS);
    const q1 = await jupQuote(USDC_MINT, SOL_MINT, inRaw);

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
      compute: cu1,
      jupiterSuggestedPriorityLamports: swap1?.prioritizationFeeLamports ?? null,
    };

    // Perna 2 usa EXATAMENTE o que a perna 1 produziu.
    const q2 = await jupQuote(SOL_MINT, USDC_MINT, q1.outAmount);

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
      compute: cu2,
      jupiterSuggestedPriorityLamports: swap2?.prioritizationFeeLamports ?? null,
    };

    sample.endUsdc = fromRaw(q2.outAmount, USDC_DECIMALS);
    sample.swapLossUsdc = sample.startUsdc - sample.endUsdc;
    sample.swapLossPct = (sample.swapLossUsdc / sample.startUsdc) * 100;

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
  console.log(`RPC      : ${RPC_URL}`);
  console.log(`Pubkey   : ${MEASURE_PUBKEY} (somente simulação, nada assinado)`);
  console.log(`Tamanhos : ${TRADE_SIZES_USDC.map((s) => "$" + s).join(", ")}`);
  console.log("");

  console.log("[1/4] Amostrando distribuição de priority fee...");
  const priorityFee = await measurePriorityFeeDistribution();
  const pf = priorityFee.solUsdc || priorityFee.global;
  if (pf) {
    console.log(
      `      ${pf.samples} slots | mediana ${pf.median} | p90 ${pf.p90} micro-lamports/CU`
    );
  } else {
    console.log(`      FALHOU: ${priorityFee.error}`);
  }

  console.log("[2/4] Medindo rent de ATA...");
  const ataRent = await measureAtaRent();
  console.log(
    ataRent.lamportsPerAccount != null
      ? `      ${ataRent.lamportsPerAccount} lamports (${(ataRent.lamportsPerAccount / LAMPORTS_PER_SOL).toFixed(9)} SOL) — recuperável`
      : `      FALHOU: ${ataRent.error}`
  );

  console.log("[3/4] Cotando preço de referência SOL/USDC (1 SOL)...");
  let solUsdcQuotedPrice = null;
  let priceError = null;
  try {
    const refQuote = await jupQuote(SOL_MINT, USDC_MINT, toRaw(1, SOL_DECIMALS));
    solUsdcQuotedPrice = fromRaw(refQuote.outAmount, USDC_DECIMALS);
    console.log(`      1 SOL = ${solUsdcQuotedPrice.toFixed(4)} USDC (cotação, não mid oracle)`);
  } catch (e) {
    priceError = e.message.slice(0, 200);
    console.log(`      FALHOU: ${priceError}`);
  }

  console.log("[4/4] Medindo round trip por tamanho...");
  const roundTrips = [];
  for (const size of TRADE_SIZES_USDC) {
    process.stdout.write(`      $${size} ... `);
    const rt = await measureRoundTrip(size, priorityFee);
    roundTrips.push(rt);
    if (rt.error) {
      console.log(`ERRO: ${rt.error}`);
    } else {
      const net = rt.networkFee?.leg1Median?.total;
      console.log(
        `perda swap ${rt.swapLossPct.toFixed(4)}% | hops ${rt.leg1.route.hops}+${rt.leg2.route.hops}` +
          (net != null ? ` | rede leg1 ${net} lamports` : " | rede n/d")
      );
    }
    await sleep(400); // respeita rate limit do RPC/Jupiter público
  }

  // Emenda 6: append com timestamp UTC e preço de cada amostra.
  const record = {
    schemaVersion: 1,
    timestampUtc: startedAt,
    finishedUtc: new Date().toISOString(),
    rpcUrl: RPC_URL,
    jupiterHost: resolvedJupHost,
    measurePubkey: MEASURE_PUBKEY,
    solUsdcQuotedPrice,
    solUsdcQuotedPriceError: priceError,
    baseFeeLamportsPerSignature: BASE_FEE_LAMPORTS_PER_SIG,
    priorityFee,
    ataRent,
    roundTrips,
  };

  mkdirSync(dirname(OUT_FILE), { recursive: true });
  appendFileSync(OUT_FILE, JSON.stringify(record) + "\n", "utf8");

  console.log("");
  console.log(`Amostra gravada em ${OUT_FILE}`);
  console.log("Rode novamente ao longo dos dias para acumular distribuição.");
  console.log("Análise: python3 analysis/day1_report.py");
}

main().catch((e) => {
  console.error("ERRO FATAL:", e.message);
  process.exit(1);
});
