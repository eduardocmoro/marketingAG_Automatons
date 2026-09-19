#!/usr/bin/env node
/**
 * TAIOS-Swarm — Day 5: spread executável entre CEX e DEX.
 *
 * MUDANÇA DE NATUREZA em relação a tudo antes disto.
 * Os 96 sinais do Day 4 tentavam PREVER o futuro. Arbitragem não prevê: o
 * lucro é travado no instante da entrada. Se a diferença supera o custo,
 * opera; se não supera, não opera. Não há acerto ou erro de direção.
 *
 * TRÊS COISAS DECIDEM SE A MEDIÇÃO PRESTA:
 *
 * 1. SIMULTANEIDADE. Binance e Jupiter são disparadas em PARALELO e o
 *    intervalo real entre as respostas fica registrado. É o mesmo erro do
 *    gap entre pernas do Day 1: amostra com intervalo grande mede deriva de
 *    preço, não spread. Amostras acima do limite são marcadas.
 *
 * 2. COMPARAÇÃO JUSTA. O preço da última negociação na Binance NÃO é
 *    comparável a uma cotação executável da Jupiter. Caminha-se o livro de
 *    ofertas para o mesmo notional, comparando preço efetivo contra preço
 *    efetivo. Usar o último preço enviesaria a favor da Binance.
 *
 * 3. A TAXA DA BINANCE DOMINA. Taker spot é ~10 bps, contra ~0,8 bps de
 *    rede em US$50. Ela é o custo principal desta operação, não a rede.
 *
 * O QUE ISTO NÃO MEDE:
 * Arbitragem de verdade exige inventário nos DOIS lados ao mesmo tempo —
 * não se transfere a cada operação. Isto mede a OPORTUNIDADE, não o custo
 * de capital parado nem o risco de a perna executar só de um lado.
 *
 * Uso:
 *   node scripts/measure-spread.mjs
 *   node scripts/measure-spread.mjs --samples 60 --interval 10
 */

import { appendFileSync, mkdirSync } from "fs";
import { execSync } from "child_process";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");
const OUT_FILE = join(ROOT, "data", "spread_cex_dex.jsonl");

const SOL_MINT = "So11111111111111111111111111111111111111112";
const USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
const SOL_DECIMALS = 9;
const USDC_DECIMALS = 6;

const JUP_HOSTS = [
  "https://lite-api.jup.ag/swap/v1",
  "https://quote-api.jup.ag/v6",
  "https://api.jup.ag/swap/v1",
];
const BINANCE_DEPTH = "https://api.binance.com/api/v3/depth";
const BINANCE_SYMBOL = "SOLUSDC";

const TIMEOUT_MS = 15_000;
// Acima disto a deriva de preco compete com o spread medido.
const MAX_VENUE_GAP_MS = 400;

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const SIZES_USDC = (arg("sizes", "5,25,50,250")).split(",").map(Number);
const SAMPLES = Number(arg("samples", "30"));
const INTERVAL_S = Number(arg("interval", "20"));
// Taker spot padrao da Binance. Ajuste se tiver desconto (BNB, VIP).
const BINANCE_TAKER_BPS = Number(arg("binance-fee-bps", "10"));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const toRaw = (a, d) => BigInt(Math.round(a * 10 ** d)).toString();
const fromRaw = (r, d) => Number(r) / 10 ** d;

function gitSha() {
  try {
    return execSync("git rev-parse --short HEAD", {
      cwd: ROOT, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return null;
  }
}
const GIT_SHA = gitSha();

let resolvedJupHost = null;

async function jupQuote(inputMint, outputMint, amountRaw) {
  const hosts = resolvedJupHost ? [resolvedJupHost] : JUP_HOSTS;
  let lastErr;
  for (const host of hosts) {
    try {
      const res = await fetch(
        `${host}/quote?inputMint=${inputMint}&outputMint=${outputMint}` +
          `&amount=${amountRaw}&slippageBps=50&onlyDirectRoutes=false`,
        { headers: { Accept: "application/json" }, signal: AbortSignal.timeout(TIMEOUT_MS) }
      );
      if (!res.ok) { lastErr = new Error(`${host} HTTP ${res.status}`); continue; }
      const j = await res.json();
      if (!j.outAmount) { lastErr = new Error(`${host} sem outAmount`); continue; }
      resolvedJupHost = host;
      return j;
    } catch (e) { lastErr = e; }
  }
  throw lastErr || new Error("Jupiter indisponivel");
}

async function binanceDepth() {
  const res = await fetch(`${BINANCE_DEPTH}?symbol=${BINANCE_SYMBOL}&limit=100`, {
    headers: { Accept: "application/json" },
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`Binance depth HTTP ${res.status}`);
  return res.json();
}

/**
 * Caminha o livro para comprar `solQty` SOL. Devolve o preco medio efetivo.
 * null se o livro nao tem profundidade suficiente — nao se extrapola.
 */
function walkAsks(asks, solQty) {
  let remaining = solQty;
  let usdc = 0;
  for (const [p, q] of asks) {
    const price = Number(p), qty = Number(q);
    if (qty >= remaining) { usdc += remaining * price; remaining = 0; break; }
    usdc += qty * price;
    remaining -= qty;
  }
  return remaining > 1e-12 ? null : usdc / solQty;
}

/** Caminha o livro para vender `solQty` SOL. Preco medio efetivo recebido. */
function walkBids(bids, solQty) {
  let remaining = solQty;
  let usdc = 0;
  for (const [p, q] of bids) {
    const price = Number(p), qty = Number(q);
    if (qty >= remaining) { usdc += remaining * price; remaining = 0; break; }
    usdc += qty * price;
    remaining -= qty;
  }
  return remaining > 1e-12 ? null : usdc / solQty;
}

async function sampleSize(sizeUsdc) {
  const inRaw = toRaw(sizeUsdc, USDC_DECIMALS);
  const t0 = Date.now();

  // PARALELO. Sequencial meteria a latencia de um dentro da medicao do outro.
  const [jupBuyRes, depthRes] = await Promise.allSettled([
    jupQuote(USDC_MINT, SOL_MINT, inRaw),
    binanceDepth(),
  ]);
  const tAfterFirst = Date.now();

  if (jupBuyRes.status !== "fulfilled" || depthRes.status !== "fulfilled") {
    return {
      sizeUsdc,
      error: [jupBuyRes.reason?.message, depthRes.reason?.message]
        .filter(Boolean).join(" | ").slice(0, 200),
    };
  }

  const jupBuy = jupBuyRes.value;
  const depth = depthRes.value;
  const solQty = fromRaw(jupBuy.outAmount, SOL_DECIMALS);
  if (!(solQty > 0)) return { sizeUsdc, error: "quote sem SOL" };

  // Venda da MESMA quantidade de SOL, para os dois lados moverem o mesmo.
  let jupSell = null;
  try {
    jupSell = await jupQuote(SOL_MINT, USDC_MINT, jupBuy.outAmount);
  } catch (e) {
    return { sizeUsdc, error: `perna de venda: ${e.message}`.slice(0, 200) };
  }
  const tEnd = Date.now();

  const jupBuyPrice = sizeUsdc / solQty;                                  // USDC por SOL
  const jupSellPrice = fromRaw(jupSell.outAmount, USDC_DECIMALS) / solQty;
  const binBuyPrice = walkAsks(depth.asks || [], solQty);
  const binSellPrice = walkBids(depth.bids || [], solQty);

  if (binBuyPrice == null || binSellPrice == null) {
    return { sizeUsdc, error: "livro da Binance raso demais para este notional" };
  }

  // Duas direcoes. Qualquer uma pode ser a oportunidade.
  const dexToCexBps = (binSellPrice / jupBuyPrice - 1) * 10_000;  // compra DEX, vende CEX
  const cexToDexBps = (jupSellPrice / binBuyPrice - 1) * 10_000;  // compra CEX, vende DEX

  return {
    sizeUsdc,
    solQty,
    jupBuyPrice, jupSellPrice,
    binBuyPrice, binSellPrice,
    // Spread interno de cada venue, util para entender de onde vem a diferenca
    jupInternalBps: (jupBuyPrice / jupSellPrice - 1) * 10_000,
    binInternalBps: (binBuyPrice / binSellPrice - 1) * 10_000,
    dexToCexBps, cexToDexBps,
    venueGapMs: tAfterFirst - t0,
    sellLegGapMs: tEnd - tAfterFirst,
    contaminated: (tAfterFirst - t0) > MAX_VENUE_GAP_MS,
    jupRoute: (jupBuy.routePlan || []).map((r) => r.swapInfo?.label).join("+") || null,
    error: null,
  };
}

async function main() {
  console.log("TAIOS-Swarm — spread executavel CEX vs DEX");
  console.log(`  par      : ${BINANCE_SYMBOL} (Binance) vs SOL/USDC (Jupiter)`);
  console.log(`  tamanhos : ${SIZES_USDC.map((s) => "$" + s).join(", ")}`);
  console.log(`  amostras : ${SAMPLES} a cada ${INTERVAL_S}s`);
  console.log(`  taxa CEX : ${BINANCE_TAKER_BPS} bps (taker spot)`);
  console.log(`  codigo   : git ${GIT_SHA ?? "?"}`);
  console.log("");
  console.log("  Positivo = oportunidade BRUTA, antes de taxa. Subtraia a taxa");
  console.log("  da Binance mais a de rede da Solana para saber se fecha.");
  console.log("");

  mkdirSync(dirname(OUT_FILE), { recursive: true });

  for (let k = 0; k < SAMPLES; k++) {
    const atUtc = new Date().toISOString();
    const results = [];
    for (const size of SIZES_USDC) {
      results.push(await sampleSize(size));
      await sleep(300); // folga entre tamanhos, nao dentro de uma amostra
    }

    appendFileSync(OUT_FILE, JSON.stringify({
      schemaVersion: 1, gitSha: GIT_SHA, timestampUtc: atUtc,
      binanceSymbol: BINANCE_SYMBOL, binanceTakerBps: BINANCE_TAKER_BPS,
      jupiterHost: resolvedJupHost, samples: results,
    }) + "\n", "utf8");

    const line = results.map((r) =>
      r.error
        ? `$${r.sizeUsdc}:erro`
        : `$${r.sizeUsdc}:${r.dexToCexBps >= 0 ? "+" : ""}${r.dexToCexBps.toFixed(1)}/` +
          `${r.cexToDexBps >= 0 ? "+" : ""}${r.cexToDexBps.toFixed(1)}` +
          (r.contaminated ? "!" : "")
    ).join("  ");
    console.log(`  [${k + 1}/${SAMPLES}] ${atUtc.slice(11, 19)}  ${line}`);

    if (k < SAMPLES - 1) await sleep(INTERVAL_S * 1000);
  }

  console.log("");
  console.log(`Gravado em ${OUT_FILE}`);
  console.log("Analise: python3 analysis/spread_report.py");
}

main().catch((e) => {
  console.error("\nERRO FATAL:", e.message);
  process.exit(1);
});
