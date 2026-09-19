#!/usr/bin/env python3
"""
TAIOS-Swarm — Day 2: candles JSONL -> SQLite.

Emenda 4: SQLite é a fronteira. O TS baixa e grava JSONL; aqui entra na base
e passa por checagem de integridade ANTES de qualquer agente rodar em cima.

Uma série com buraco não anunciado vira "edge" falso: o agente atravessa o
buraco como se fosse um movimento instantâneo e captura um retorno que nunca
existiu. Por isso a checagem de lacunas vem antes de tudo.

Uso:
    python3 analysis/ingest_candles.py
    python3 analysis/ingest_candles.py --jsonl data/candles_SOLUSDC_1m.jsonl
"""

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "market.sqlite"

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS candle (
    symbol        TEXT    NOT NULL,
    interval      TEXT    NOT NULL,
    open_time_ms  INTEGER NOT NULL,
    open          REAL    NOT NULL,
    high          REAL    NOT NULL,
    low           REAL    NOT NULL,
    close         REAL    NOT NULL,
    volume        REAL,
    quote_volume  REAL,
    trades        INTEGER,
    close_time_ms INTEGER,
    taker_buy_base  REAL,   -- volume comprador agressor (fluxo de ordens)
    taker_buy_quote REAL,
    source        TEXT,
    PRIMARY KEY (symbol, interval, open_time_ms)
);
CREATE INDEX IF NOT EXISTS idx_candle_time
    ON candle(symbol, interval, open_time_ms);
"""


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def ingest(jsonl: Path, db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    for col, decl in [("taker_buy_base", "REAL"), ("taker_buy_quote", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE candle ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # coluna já existe

    if not jsonl.exists():
        print(f"ERRO: {jsonl} não existe. Rode scripts/fetch-history.mjs primeiro.")
        sys.exit(1)

    inserted = skipped = malformed = 0
    with open(jsonl, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1  # linha truncada por interrupção
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO candle
                   (symbol, interval, open_time_ms, open, high, low, close,
                    volume, quote_volume, trades, close_time_ms,
                    taker_buy_base, taker_buy_quote, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    c.get("symbol"), c.get("interval"), c.get("openTimeMs"),
                    c.get("open"), c.get("high"), c.get("low"), c.get("close"),
                    c.get("volume"), c.get("quoteVolume"), c.get("trades"),
                    c.get("closeTimeMs"), c.get("takerBuyBase"),
                    c.get("takerBuyQuote"), c.get("source"),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1

    conn.commit()
    print(f"Ingestão: {inserted:,} candles novos, {skipped:,} já existentes"
          + (f", {malformed} linha(s) malformada(s)" if malformed else ""))
    return conn


def integrity(conn: sqlite3.Connection) -> dict:
    out = {}
    series = conn.execute(
        "SELECT symbol, interval, COUNT(*), MIN(open_time_ms), MAX(open_time_ms) "
        "FROM candle GROUP BY symbol, interval"
    ).fetchall()

    print()
    print("=" * 78)
    print("  INTEGRIDADE DA SERIE")
    print("=" * 78)

    for symbol, interval, n, t0, t1 in series:
        step = INTERVAL_MS.get(interval)
        print()
        print(f"  {symbol} {interval}")
        print(f"    candles  : {n:,}")
        print(f"    de       : {iso(t0)}")
        print(f"    ate      : {iso(t1)}")

        if not step:
            print(f"    [!] intervalo desconhecido — checagem de lacuna pulada")
            continue

        expected = (t1 - t0) // step + 1
        missing = expected - n
        print(f"    esperados: {expected:,} (faltam {missing:,}, "
              f"{missing/expected*100:.3f}%)")

        # Lacunas: candle seguinte a mais de um passo de distancia.
        times = [r[0] for r in conn.execute(
            "SELECT open_time_ms FROM candle WHERE symbol=? AND interval=? "
            "ORDER BY open_time_ms", (symbol, interval))]
        gaps = []
        for a, b in zip(times, times[1:]):
            if b - a > step:
                gaps.append((a, b, (b - a) // step - 1))

        if gaps:
            gaps.sort(key=lambda g: -g[2])
            print(f"    lacunas  : {len(gaps)} trecho(s)")
            for a, b, n_missing in gaps[:5]:
                print(f"      {iso(a)[:16]} -> {iso(b)[:16]}  ({n_missing} candles)")
            if len(gaps) > 5:
                print(f"      ... e mais {len(gaps)-5}")
            print()
            print("    ATENCAO: uma lacuna nao tratada vira edge falso. O agente")
            print("    atravessa o buraco como se fosse movimento instantaneo e")
            print("    captura retorno que nunca existiu. O backtest precisa")
            print("    interromper a posicao na lacuna, nao interpolar.")
        else:
            print(f"    lacunas  : nenhuma")

        # Sanidade de preco
        bad = conn.execute(
            "SELECT COUNT(*) FROM candle WHERE symbol=? AND interval=? AND ("
            "  high < low OR close <= 0 OR open <= 0"
            "  OR close > high OR close < low OR open > high OR open < low)",
            (symbol, interval)).fetchone()[0]
        print(f"    OHLC inconsistente: {bad}")

        closes = [r[0] for r in conn.execute(
            "SELECT close FROM candle WHERE symbol=? AND interval=? "
            "ORDER BY open_time_ms", (symbol, interval))]
        if len(closes) > 2:
            rets = [(b / a - 1) * 10_000 for a, b in zip(closes, closes[1:]) if a > 0]
            absr = sorted(abs(r) for r in rets)
            p50 = statistics.median(absr)
            p90 = absr[int(len(absr) * 0.90)]
            p99 = absr[int(len(absr) * 0.99)]
            print(f"    |retorno| por candle: p50 {p50:.2f} bps | "
                  f"p90 {p90:.2f} | p99 {p99:.2f} | max {absr[-1]:.1f}")

            out[f"{symbol}_{interval}"] = {
                "candles": n, "missing": missing, "gaps": len(gaps),
                "ohlc_bad": bad,
                "abs_return_bps": {"p50": p50, "p90": p90, "p99": p99, "max": absr[-1]},
                "from": iso(t0), "to": iso(t1),
            }

    print()
    print("=" * 78)
    print("  O QUE ISTO JA DIZ SOBRE A TESE")
    print("=" * 78)
    print()
    print("  Break-even medido no Day 1, posicao de US$5: ~0,085% = 8,5 bps.")
    print("  Compare com o |retorno| por candle acima: se o p90 de um candle de")
    print("  1 minuto for muito menor que 8,5 bps, uma estrategia precisa de")
    print("  varios candles de movimento na MESMA direcao para pagar o custo —")
    print("  e a chance disso acontecer por acaso e o que o controle vai medir.")
    print()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args()

    jsonl = args.jsonl
    if jsonl is None:
        found = sorted((ROOT / "data").glob("candles_*.jsonl"))
        if not found:
            print("ERRO: nenhum data/candles_*.jsonl. Rode scripts/fetch-history.mjs.")
            sys.exit(1)
        jsonl = found[0]
        if len(found) > 1:
            print(f"Varios arquivos encontrados; usando {jsonl.name}. "
                  f"Use --jsonl para escolher outro.")

    conn = ingest(jsonl, args.db)
    summary = integrity(conn)
    out = args.db.parent / "market_summary.json"
    out.write_text(json.dumps(summary, indent=2), "utf-8")
    print(f"  Resumo: {out}")
    conn.close()


if __name__ == "__main__":
    main()
