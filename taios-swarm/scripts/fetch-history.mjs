#!/usr/bin/env node
/**
 * TAIOS-Swarm — Day 2: histórico de preço SOL/USDC
 *
 * Baixa candles de 1 minuto da Binance e grava em JSONL. O Python ingere
 * para SQLite depois (emenda 4: SQLite é a fronteira).
 *
 * POR QUE BINANCE, e a ressalva:
 * É preço de venue CENTRALIZADO, não o preço on-chain que a Jupiter cota.
 * Serve para responder "existe movimento direcional previsível acima do
 * break-even?", porque é esse fluxo que arrasta o preço on-chain. NÃO serve
 * para medir custo de execução — esse já foi medido no Day 1, separadamente,
 * contra a Jupiter e o RPC.
 *
 * Uso:
 *   node scripts/fetch-history.mjs                 # 90 dias de SOLUSDC 1m
 *   node scripts/fetch-history.mjs --days 180
 *   node scripts/fetch-history.mjs --symbol SOLUSDT --interval 5m
 *
 * Append-only e idempotente: rodar de novo só acrescenta o que falta.
 */

import { appendFileSync, mkdirSync, existsSync, readFileSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");

const BINANCE = "https://api.binance.com/api/v3/klines";
const MAX_PER_REQUEST = 1000; // teto da API
const REQUEST_GAP_MS = 250; // folga sobre o limite de peso da Binance
const TIMEOUT_MS = 20_000;

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const SYMBOL = arg("symbol", "SOLUSDC");
const INTERVAL = arg("interval", "1m");
const DAYS = Number(arg("days", "90"));

const OUT_DIR = join(ROOT, "data");
const OUT_FILE = join(OUT_DIR, `candles_${SYMBOL}_${INTERVAL}.jsonl`);

const INTERVAL_MS = {
  "1m": 60_000,
  "3m": 180_000,
  "5m": 300_000,
  "15m": 900_000,
  "1h": 3_600_000,
  "4h": 14_400_000,
  "1d": 86_400_000,
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Maior openTime já gravado, para continuar de onde parou. */
function lastOpenTime() {
  if (!existsSync(OUT_FILE)) return null;
  const lines = readFileSync(OUT_FILE, "utf8").trim().split("\n");
  let max = null;
  // Varre do fim: o arquivo costuma estar em ordem, mas não se pode assumir.
  for (let i = lines.length - 1; i >= 0 && i > lines.length - 50; i--) {
    try {
      const t = JSON.parse(lines[i]).openTimeMs;
      if (t != null && (max == null || t > max)) max = t;
    } catch {
      // linha truncada por interrupção — ignora, o Python também ignora
    }
  }
  return max;
}

async function fetchChunk(startTime) {
  const url =
    `${BINANCE}?symbol=${SYMBOL}&interval=${INTERVAL}` +
    `&startTime=${startTime}&limit=${MAX_PER_REQUEST}`;
  const res = await fetch(url, {
    headers: { Accept: "application/json" },
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (res.status === 429 || res.status === 418) {
    throw new Error(`rate limit da Binance (HTTP ${res.status}) — aumente REQUEST_GAP_MS`);
  }
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
  }
  return res.json();
}

async function main() {
  const step = INTERVAL_MS[INTERVAL];
  if (!step) {
    console.error(`Intervalo desconhecido: ${INTERVAL}. Use ${Object.keys(INTERVAL_MS).join(", ")}`);
    process.exit(1);
  }

  const now = Date.now();
  const resume = lastOpenTime();
  const startTime = resume != null ? resume + step : now - DAYS * 86_400_000;

  console.log("TAIOS-Swarm — histórico de preço");
  console.log(`  par      : ${SYMBOL} (Binance, venue CENTRALIZADO — ver ressalva no topo)`);
  console.log(`  intervalo: ${INTERVAL}`);
  console.log(`  de       : ${new Date(startTime).toISOString()}`);
  console.log(`  até      : ${new Date(now).toISOString()}`);
  if (resume != null) {
    console.log(`  (continuando de ${new Date(resume).toISOString()})`);
  }
  const estimated = Math.max(0, Math.ceil((now - startTime) / step));
  console.log(`  candles estimados: ~${estimated.toLocaleString("pt-BR")}`);
  console.log("");

  if (estimated === 0) {
    console.log("Nada novo a baixar.");
    return;
  }

  mkdirSync(OUT_DIR, { recursive: true });

  let cursor = startTime;
  let written = 0;
  let requests = 0;

  while (cursor < now) {
    let batch;
    try {
      batch = await fetchChunk(cursor);
    } catch (e) {
      console.error(`\nERRO na requisição ${requests + 1}: ${e.message}`);
      console.error(`Gravados ${written} candles. Rode de novo para continuar daqui.`);
      process.exit(1);
    }
    requests++;
    if (!Array.isArray(batch) || batch.length === 0) break;

    const rows = batch.map((k) => ({
      symbol: SYMBOL,
      interval: INTERVAL,
      openTimeMs: k[0],
      open: Number(k[1]),
      high: Number(k[2]),
      low: Number(k[3]),
      close: Number(k[4]),
      volume: Number(k[5]),
      closeTimeMs: k[6],
      quoteVolume: Number(k[7]),
      trades: k[8],
      source: "binance",
    }));

    appendFileSync(OUT_FILE, rows.map((r) => JSON.stringify(r)).join("\n") + "\n", "utf8");
    written += rows.length;
    cursor = batch[batch.length - 1][0] + step;

    process.stdout.write(
      `\r  ${written.toLocaleString("pt-BR")} candles | ` +
        `${requests} requisições | até ${new Date(cursor).toISOString().slice(0, 16)}`
    );

    if (batch.length < MAX_PER_REQUEST) break; // alcançou o presente
    await sleep(REQUEST_GAP_MS);
  }

  console.log("");
  console.log("");
  console.log(`Gravado em ${OUT_FILE}`);
  console.log(`Total desta execução: ${written.toLocaleString("pt-BR")} candles`);
  console.log("Próximo: python3 analysis/ingest_candles.py");
}

main().catch((e) => {
  console.error("\nERRO FATAL:", e.message);
  process.exit(1);
});
