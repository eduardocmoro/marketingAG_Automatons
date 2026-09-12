#!/usr/bin/env python3
"""
TAIOS-Swarm — Day 1: relatório de custo de round trip.

Emenda 4: SQLite é a fronteira. O TS mede e grava; toda estatística
(distribuição, mediana, p90, IC) acontece aqui em Python.

Fluxo:  measurements/day1_costs.jsonl  ->  measurements/day1.sqlite  ->  tabela

Uso:
    python3 analysis/day1_report.py
    python3 analysis/day1_report.py --jsonl caminho/day1_costs.jsonl

Só usa stdlib. Sem dependências.
"""

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JSONL = ROOT / "measurements" / "day1_costs.jsonl"
DEFAULT_DB = ROOT / "measurements" / "day1.sqlite"

LAMPORTS_PER_SOL = 1_000_000_000

# Mints distintos que o enxame negocia. Sob ADR-001 (carteira única) é
# também o número total de ATAs do projeto inteiro — uma vez, para sempre.
DEFAULT_CORE_MINTS = 5

# Emenda: cobertura mínima para a distribuição de congestionamento fazer sentido.
MIN_WINDOWS = 3
MIN_SPAN_HOURS = 24
WINDOW_GAP_HOURS = 1  # intervalo que separa duas janelas distintas

# Item 4: pelo menos uma janela precisa cair em atividade alta. O priority
# fee mediano da janela é o medidor. Exigir que a janela mais congestionada
# seja ao menos este múltiplo da mais calma.
MIN_CONGESTION_SPREAD = 2.0


# ── Ingestão: JSONL -> SQLite ────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc       TEXT NOT NULL,
    rpc_url             TEXT,
    jupiter_host        TEXT,
    sol_usdc_price      REAL,
    base_fee_per_sig    INTEGER,
    cu_price_median     REAL,
    cu_price_p90        REAL,
    cu_price_samples    INTEGER,
    ata_rent_lamports   INTEGER,
    UNIQUE(timestamp_utc)
);

CREATE TABLE IF NOT EXISTS round_trip (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  INTEGER NOT NULL REFERENCES run(run_id),
    position_size_usdc      REAL NOT NULL,
    start_usdc              REAL,
    end_usdc                REAL,
    swap_loss_usdc          REAL,
    swap_loss_pct           REAL,
    leg1_quoted_impact_pct  REAL,
    leg2_quoted_impact_pct  REAL,
    leg1_realized_slip_pct  REAL,   -- FASE 2 apenas; null no paper
    leg2_realized_slip_pct  REAL,   -- FASE 2 apenas; null no paper
    leg1_hops               INTEGER,
    leg2_hops               INTEGER,
    leg1_venues             TEXT,
    leg2_venues             TEXT,
    net_lamports_median     INTEGER,
    net_lamports_p90        INTEGER,
    cu_consumed_source      TEXT,
    tx_failure_rate         REAL,   -- FASE 2 apenas; null no paper
    error                   TEXT
);

CREATE TABLE IF NOT EXISTS quote_drift (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id               INTEGER NOT NULL REFERENCES run(run_id),
    position_size_usdc   REAL NOT NULL,
    nominal_horizon_ms   INTEGER NOT NULL,
    elapsed_ms_request   INTEGER,
    elapsed_ms_response  INTEGER,
    drift_bps            REAL,
    error                TEXT
);
"""

# Horizontes usados nas colunas de break-even sensível à latência.
BREAK_EVEN_HORIZONS_MS = [1000, 2000]


def ingest(jsonl_path: Path, db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    # Bases criadas antes destas colunas continuam utilizáveis.
    for table, col, decl in [("round_trip", "tx_failure_rate", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # coluna já existe

    if not jsonl_path.exists():
        print(f"ERRO: {jsonl_path} não existe. Rode scripts/measure-day1.mjs primeiro.")
        sys.exit(1)

    inserted_runs = 0
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"  aviso: linha {line_no} inválida ({e}), ignorada")
                continue

            pf = rec.get("priorityFee") or {}
            scoped = pf.get("solUsdc") or pf.get("global") or {}

            cur = conn.execute(
                """INSERT OR IGNORE INTO run
                   (timestamp_utc, rpc_url, jupiter_host, sol_usdc_price,
                    base_fee_per_sig, cu_price_median, cu_price_p90,
                    cu_price_samples, ata_rent_lamports)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    rec.get("timestampUtc"),
                    rec.get("rpcUrl"),
                    rec.get("jupiterHost"),
                    rec.get("solUsdcQuotedPrice"),
                    rec.get("baseFeeLamportsPerSignature"),
                    scoped.get("median"),
                    scoped.get("p90"),
                    scoped.get("samples"),
                    (rec.get("ataRent") or {}).get("lamportsPerAccount"),
                ),
            )
            if cur.rowcount == 0:
                continue  # run já ingerida
            inserted_runs += 1
            run_id = cur.lastrowid

            for rt in rec.get("roundTrips", []):
                leg1 = rt.get("leg1") or {}
                leg2 = rt.get("leg2") or {}
                nf = rt.get("networkFee") or {}

                def total(key):
                    v = nf.get(key)
                    return v.get("total") if isinstance(v, dict) else None

                l1m, l2m = total("leg1Median"), total("leg2Median")
                l1p, l2p = total("leg1P90"), total("leg2P90")

                def venues(leg):
                    route = (leg or {}).get("route") or {}
                    return "+".join(
                        str(h.get("venue")) for h in route.get("legs", []) if h.get("venue")
                    ) or None

                conn.execute(
                    """INSERT INTO round_trip
                       (run_id, position_size_usdc, start_usdc, end_usdc,
                        swap_loss_usdc, swap_loss_pct,
                        leg1_quoted_impact_pct, leg2_quoted_impact_pct,
                        leg1_realized_slip_pct, leg2_realized_slip_pct,
                        leg1_hops, leg2_hops, leg1_venues, leg2_venues,
                        net_lamports_median, net_lamports_p90,
                        cu_consumed_source, tx_failure_rate, error)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        rt.get("positionSizeUsdc"),
                        rt.get("startUsdc"),
                        rt.get("endUsdc"),
                        rt.get("swapLossUsdc"),
                        rt.get("swapLossPct"),
                        leg1.get("quotedPriceImpactPct"),
                        leg2.get("quotedPriceImpactPct"),
                        leg1.get("realizedSlippagePct"),  # null na Fase 1, por construção
                        leg2.get("realizedSlippagePct"),
                        (leg1.get("route") or {}).get("hops"),
                        (leg2.get("route") or {}).get("hops"),
                        venues(leg1),
                        venues(leg2),
                        (l1m + l2m) if (l1m is not None and l2m is not None) else None,
                        (l1p + l2p) if (l1p is not None and l2p is not None) else None,
                        (leg1.get("compute") or {}).get("cuConsumedSource"),
                        rt.get("txFailureRate"),  # null na Fase 1, por construção
                        rt.get("error"),
                    ),
                )

            for drift in rec.get("quoteDrift", []):
                size = drift.get("positionSizeUsdc")
                if drift.get("error"):
                    conn.execute(
                        """INSERT INTO quote_drift
                           (run_id, position_size_usdc, nominal_horizon_ms, error)
                           VALUES (?,?,?,?)""",
                        (run_id, size, -1, drift["error"]),
                    )
                    continue
                for pt in drift.get("points", []):
                    conn.execute(
                        """INSERT INTO quote_drift
                           (run_id, position_size_usdc, nominal_horizon_ms,
                            elapsed_ms_request, elapsed_ms_response, drift_bps, error)
                           VALUES (?,?,?,?,?,?,?)""",
                        (
                            run_id,
                            size,
                            pt.get("nominalHorizonMs"),
                            pt.get("elapsedMsAtRequest"),
                            pt.get("elapsedMsAtResponse"),
                            pt.get("driftBps"),
                            pt.get("error"),
                        ),
                    )

    conn.commit()
    print(f"Ingestão: {inserted_runs} nova(s) execução(ões) -> {db_path}")
    return conn


# ── Estatística ──────────────────────────────────────────────────────


def pct(values, p):
    """Percentil por interpolação linear. Devolve None se vazio."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    idx = (len(vals) - 1) * p
    lo, hi = int(idx), min(int(idx) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)


def fmt(v, nd=4, dash="—"):
    return dash if v is None else f"{v:.{nd}f}"


def drift_stats(conn, size=None):
    """
    |drift| por horizonte de latência.

    A mediana COM SINAL tende a zero (o preço anda para os dois lados).
    O custo está na MAGNITUDE: metade das vezes o movimento é adverso, e é
    esse caso que define o break-even conservador.
    """
    q = (
        "SELECT nominal_horizon_ms, drift_bps FROM quote_drift "
        "WHERE drift_bps IS NOT NULL AND nominal_horizon_ms > 0"
    )
    params = []
    if size is not None:
        q += " AND position_size_usdc = ?"
        params.append(size)

    by_horizon = {}
    for h, d in conn.execute(q, params):
        by_horizon.setdefault(h, []).append(d)

    out = {}
    for h, vals in by_horizon.items():
        absv = [abs(v) for v in vals]
        out[h] = {
            "n": len(vals),
            "median_signed": statistics.median(vals),
            "median_abs": statistics.median(absv),
            "p90_abs": pct(absv, 0.9),
        }
    return out


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def coverage(conn):
    """
    Agrupa as execuções em janelas (intervalo > WINDOW_GAP_HOURS separa janelas).

    Uma hora corrida de amostras é UM regime de congestionamento, não uma
    distribuição. Sem janelas espalhadas, mediana e p90 descrevem aquela hora
    e nada além dela.
    """
    rows = [
        (parse_ts(r[0]), r[1])
        for r in conn.execute(
            "SELECT timestamp_utc, cu_price_median FROM run ORDER BY timestamp_utc"
        )
    ]
    rows = [(t, c) for t, c in rows if t is not None]
    if not rows:
        return {
            "windows": [],
            "span_hours": 0.0,
            "hours_of_day": set(),
            "sufficient": False,
            "congestion_spread": None,
            "congestion_varied": False,
        }

    windows = [[rows[0]]]
    for row in rows[1:]:
        if (row[0] - windows[-1][-1][0]) > timedelta(hours=WINDOW_GAP_HOURS):
            windows.append([row])
        else:
            windows[-1].append(row)

    stamps = [t for t, _ in rows]
    span_hours = (stamps[-1] - stamps[0]).total_seconds() / 3600

    # Item 4: três janelas em horário morto passam o gate de tempo e mentem
    # no p90. O próprio priority fee é o medidor de atividade on-chain —
    # exigir que as janelas cubram regimes de congestionamento diferentes.
    win_congestion = []
    for w in windows:
        vals = [c for _, c in w if c is not None]
        win_congestion.append(statistics.median(vals) if vals else None)

    valid = [c for c in win_congestion if c is not None and c > 0]
    spread = (max(valid) / min(valid)) if len(valid) >= 2 else None
    congestion_varied = spread is not None and spread >= MIN_CONGESTION_SPREAD

    return {
        "windows": windows,
        "window_congestion": win_congestion,
        "span_hours": span_hours,
        "hours_of_day": {s.hour for s in stamps},
        "sufficient": (
            len(windows) >= MIN_WINDOWS and span_hours >= MIN_SPAN_HOURS and congestion_varied
        ),
        "enough_windows": len(windows) >= MIN_WINDOWS and span_hours >= MIN_SPAN_HOURS,
        "congestion_spread": spread,
        "congestion_varied": congestion_varied,
    }


def report(conn: sqlite3.Connection, core_mints: int = DEFAULT_CORE_MINTS,
           out_dir: Path = ROOT / "measurements") -> None:
    runs = conn.execute(
        "SELECT COUNT(*), MIN(timestamp_utc), MAX(timestamp_utc) FROM run"
    ).fetchone()
    n_runs, first, last = runs

    ok = conn.execute("SELECT COUNT(*) FROM round_trip WHERE error IS NULL").fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM round_trip WHERE error IS NOT NULL").fetchone()[0]

    print()
    print("=" * 78)
    print("  TAIOS-SWARM — DAY 1: CUSTO REAL DE ROUND TRIP")
    print("=" * 78)
    print(f"  Execuções      : {n_runs}   ({first}  ->  {last})")
    print(f"  Amostras       : {ok} válidas, {failed} com erro")

    if ok == 0:
        print()
        print("  NENHUMA AMOSTRA VÁLIDA. Erros registrados:")
        for (err,) in conn.execute(
            "SELECT DISTINCT error FROM round_trip WHERE error IS NOT NULL LIMIT 10"
        ):
            print(f"    - {err}")
        print()
        print("  Sem dado real não há tabela. Corrija o acesso de rede e rode de novo.")
        print("=" * 78)
        return

    prices = [
        r[0] for r in conn.execute("SELECT sol_usdc_price FROM run WHERE sol_usdc_price IS NOT NULL")
    ]
    cu_med = [r[0] for r in conn.execute("SELECT cu_price_median FROM run WHERE cu_price_median IS NOT NULL")]
    cu_p90 = [r[0] for r in conn.execute("SELECT cu_price_p90 FROM run WHERE cu_price_p90 IS NOT NULL")]
    rents = [r[0] for r in conn.execute("SELECT ata_rent_lamports FROM run WHERE ata_rent_lamports IS NOT NULL")]

    sol_price = statistics.median(prices) if prices else None
    print(f"  SOL/USDC       : {fmt(sol_price, 4)} (mediana das cotações)")
    if cu_med:
        print(
            f"  Priority fee   : mediana {fmt(statistics.median(cu_med), 0)} | "
            f"p90 {fmt(statistics.median(cu_p90), 0) if cu_p90 else '—'} micro-lamports/CU"
        )
    if rents and sol_price:
        rent_sol = statistics.median(rents) / LAMPORTS_PER_SOL
        print(
            f"  Rent por ATA   : {statistics.median(rents):,.0f} lamports = "
            f"{rent_sol:.9f} SOL = ${rent_sol * sol_price:.4f}"
        )

    # ── Cobertura temporal ──
    cov = coverage(conn)
    n_win = len(cov["windows"])
    print(
        f"  Cobertura      : {n_win} janela(s), {cov['span_hours']:.1f}h de span, "
        f"horas UTC {sorted(cov['hours_of_day'])}"
    )
    spread = cov["congestion_spread"]
    wc = [f"{c:.0f}" if c is not None else "?" for c in (cov.get("window_congestion") or [])]
    print(
        f"  Congestionam.  : por janela {wc}"
        + (f" | spread {spread:.1f}x" if spread is not None else " | spread n/d")
    )

    if not cov["sufficient"]:
        print()
        print("  " + "!" * 72)
        if not cov["enough_windows"]:
            print(f"  ATENCAO: cobertura temporal insuficiente ({n_win} janela(s), "
                  f"{cov['span_hours']:.1f}h).")
            print(f"  Minimo: {MIN_WINDOWS} janelas em >= {MIN_SPAN_HOURS}h.")
        if not cov["congestion_varied"]:
            print(f"  ATENCAO: janelas em regime de congestionamento parecido"
                  + (f" (spread {spread:.1f}x, minimo {MIN_CONGESTION_SPREAD:.1f}x)."
                     if spread is not None else " (spread indisponivel)."))
            print("  Tres janelas em horario morto passam no gate de tempo e MENTEM no p90.")
            print("  Rode um bloco em periodo de alta atividade on-chain.")
        print("  Mediana e p90 abaixo descrevem as janelas medidas, nao o regime geral.")
        print("  " + "!" * 72)

    # ── Tabela principal ──
    print()
    print("-" * 78)
    print("  CUSTO AFUNDADO DE ROUND TRIP POR TAMANHO DE POSIÇÃO")
    print("  (perda de swap = fee de pool + price impact cotado, ida e volta)")
    print("-" * 78)
    print()
    header = (
        f"  {'POSIÇÃO':>9} | {'SWAP LOSS':>10} | {'REDE':>9} | "
        f"{'TOTAL':>9} | {'% POSIÇÃO':>9} | {'BREAK-EVEN':>10}"
    )
    print(header)
    print(f"  {'':>9} | {'mediana $':>10} | {'mediana $':>9} | "
          f"{'mediana $':>9} | {'mediana':>9} | {'movimento':>10}")
    print("  " + "-" * 74)

    summary = []
    sizes = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT position_size_usdc FROM round_trip "
            "WHERE error IS NULL ORDER BY position_size_usdc"
        )
    ]

    for size in sizes:
        rows = conn.execute(
            """SELECT swap_loss_usdc, swap_loss_pct, net_lamports_median, net_lamports_p90
               FROM round_trip WHERE position_size_usdc = ? AND error IS NULL""",
            (size,),
        ).fetchall()

        swap_losses = [r[0] for r in rows if r[0] is not None]
        net_med = [r[2] for r in rows if r[2] is not None]
        net_p90 = [r[3] for r in rows if r[3] is not None]

        swap_med = pct(swap_losses, 0.5)
        swap_hi = pct(swap_losses, 0.9)

        net_usd_med = (
            (pct(net_med, 0.5) / LAMPORTS_PER_SOL) * sol_price
            if net_med and sol_price
            else None
        )
        net_usd_p90 = (
            (pct(net_p90, 0.9) / LAMPORTS_PER_SOL) * sol_price
            if net_p90 and sol_price
            else None
        )

        total_med = (swap_med + net_usd_med) if (swap_med is not None and net_usd_med is not None) else None
        total_p90 = (swap_hi + net_usd_p90) if (swap_hi is not None and net_usd_p90 is not None) else None

        pct_med = (total_med / size * 100) if total_med is not None else None
        pct_p90 = (total_p90 / size * 100) if total_p90 is not None else None

        print(
            f"  {'$' + format(size, 'g'):>9} | {fmt(swap_med, 6):>10} | {fmt(net_usd_med, 6):>9} | "
            f"{fmt(total_med, 6):>9} | {fmt(pct_med, 3) + '%':>9} | {fmt(pct_med, 3) + '%':>10}"
        )

        summary.append(
            {
                "position_usdc": size,
                "samples": len(rows),
                "swap_loss_median_usdc": swap_med,
                "swap_loss_p90_usdc": swap_hi,
                "network_median_usdc": net_usd_med,
                "network_p90_usdc": net_usd_p90,
                "total_sunk_median_usdc": total_med,
                "total_sunk_p90_usdc": total_p90,
                "total_sunk_median_pct": pct_med,
                "total_sunk_p90_pct": pct_p90,
                "break_even_move_median_pct": pct_med,
                "break_even_move_p90_pct": pct_p90,
            }
        )

    # ── Cenário p90 ──
    print()
    print("  " + "-" * 74)
    print(f"  {'POSIÇÃO':>9} | {'TOTAL p90 $':>12} | {'% POSIÇÃO p90':>14} | {'BREAK-EVEN p90':>15}")
    print("  " + "-" * 74)
    for s in summary:
        print(
            f"  {'$' + format(s['position_usdc'], 'g'):>9} | {fmt(s['total_sunk_p90_usdc'], 6):>12} | "
            f"{fmt(s['total_sunk_p90_pct'], 3) + '%':>14} | {fmt(s['break_even_move_p90_pct'], 3) + '%':>15}"
        )

    # ── Drift por latência: o custo do TEMPO ──
    dstats = drift_stats(conn)
    print()
    print("-" * 78)
    print("  DRIFT DE COTACAO POR LATENCIA — O CUSTO DO TEMPO")
    print("  (mesma quote, mesmo par e tamanho, recotada apos cada horizonte)")
    print("-" * 78)
    if not dstats:
        print("  Sem amostras de drift nesta base.")
    else:
        print()
        print(f"  {'HORIZONTE':>10} | {'n':>5} | {'MEDIANA':>10} | {'|DRIFT| MED':>12} | {'|DRIFT| p90':>12}")
        print(f"  {'':>10} | {'':>5} | {'com sinal':>10} | {'bps':>12} | {'bps':>12}")
        print("  " + "-" * 62)
        for h in sorted(dstats):
            d = dstats[h]
            print(
                f"  {str(h) + 'ms':>10} | {d['n']:>5} | "
                f"{fmt(d['median_signed'], 2):>10} | {fmt(d['median_abs'], 2):>12} | "
                f"{fmt(d['p90_abs'], 2):>12}"
            )
        print()
        print("  A mediana com sinal tende a zero — o preco anda para os dois lados.")
        print("  O que custa e a MAGNITUDE: metade das vezes ela joga contra.")

        # ── Break-even sensivel a velocidade do loop ──
        print()
        print("-" * 78)
        print("  BREAK-EVEN SENSIVEL A VELOCIDADE DO LOOP")
        print("  break_even = swapLoss + rede + |drift(latencia)|   (caso adverso)")
        print("-" * 78)
        print()
        cols = " | ".join(f"{'+' + str(h) + 'ms':>11}" for h in BREAK_EVEN_HORIZONS_MS)
        print(f"  {'POSIÇÃO':>9} | {'SEM DRIFT':>11} | {cols}")
        print("  " + "-" * 62)
        for s in summary:
            cells = []
            for h in BREAK_EVEN_HORIZONS_MS:
                d = dstats.get(h)
                base_be = s["break_even_move_median_pct"]
                if d is None or base_be is None or d["median_abs"] is None:
                    cells.append(f"{'—':>11}")
                else:
                    be = base_be + d["median_abs"] / 100
                    s[f"break_even_with_drift_{h}ms_pct"] = be
                    cells.append(f"{fmt(be, 3) + '%':>11}")
            print(
                f"  {'$' + format(s['position_usdc'], 'g'):>9} | "
                f"{fmt(s['break_even_move_median_pct'], 3) + '%':>11} | " + " | ".join(cells)
            )

    # ── Leitura direta ──
    one = next((s for s in summary if abs(s["position_usdc"] - 1.0) < 1e-9), None)
    print()
    print("=" * 78)
    print("  LEITURA DIRETA — A PERGUNTA QUE DECIDE O PROJETO")
    print("=" * 78)
    if one and one["break_even_move_median_pct"] is not None:
        print()
        print(f"  Numa posição de US$1,00:")
        print(f"    custo afundado do round trip : ${one['total_sunk_median_usdc']:.6f} (mediana)")
        if one["total_sunk_p90_usdc"] is not None:
            print(f"                                   ${one['total_sunk_p90_usdc']:.6f} (p90)")
        print()
        print(f"    MOVIMENTO DE PREÇO NECESSÁRIO SÓ PARA EMPATAR:")
        print(f"      mediana : {one['break_even_move_median_pct']:.3f}%")
        if one["break_even_move_p90_pct"] is not None:
            print(f"      p90     : {one['break_even_move_p90_pct']:.3f}%")
        print()
        print("    O rent de ATA NAO entra nesta conta. Ver bloco de setup abaixo.")
    else:
        print("  Sem amostra válida de US$1,00. Rode a medição com esse tamanho.")
    print()
    print("=" * 78)

    # ── Setup de portfólio: uma vez, não por trade (ADR-001 §3) ──
    if rents and sol_price:
        rent_lamports = statistics.median(rents)
        rent_usd = rent_lamports / LAMPORTS_PER_SOL * sol_price
        total_usd = rent_usd * core_mints
        print()
        print("  SETUP DE PORTFOLIO — UMA VEZ, NAO POR TRADE")
        print("  " + "-" * 74)
        print(f"    ATA e criada uma vez por (carteira, mint) e reusada por todos os")
        print(f"    trades seguintes, de todos os agentes. Sob ADR-001 o enxame inteiro")
        print(f"    usa UMA carteira, entao sao {core_mints} ATAs no total, para sempre.")
        print()
        print(f"    rent por ATA          : ${rent_usd:.4f}  (SOL medido a ${sol_price:.2f})")
        print(f"    {core_mints} mints do nucleo     : ${total_usd:.4f} travados, UMA vez")
        print(f"    custo por trade       : $0.0000 — nao escala com numero de trades")
        print()
        print("    O rent e CAPITAL TRAVADO RECUPERAVEL: volta integralmente ao fechar")
        print("    a conta. Nao e custo afundado e nao entra no break-even por trade.")

    # ── Rotas observadas ──
    print()
    print("  ROTAS OBSERVADAS (do routePlan real, não fixadas)")
    print("  " + "-" * 74)
    for size, v1, v2, h1, h2, n in conn.execute(
        """SELECT position_size_usdc, leg1_venues, leg2_venues, leg1_hops, leg2_hops, COUNT(*)
           FROM round_trip WHERE error IS NULL
           GROUP BY position_size_usdc, leg1_venues, leg2_venues
           ORDER BY position_size_usdc"""
    ):
        print(f"  ${format(size, 'g'):<7} USDC->SOL [{h1}] {v1 or '?'}  |  SOL->USDC [{h2}] {v2 or '?'}  (n={n})")

    # ── Garantia da emenda 3 ──
    leaked = conn.execute(
        "SELECT COUNT(*) FROM round_trip "
        "WHERE leg1_realized_slip_pct IS NOT NULL OR leg2_realized_slip_pct IS NOT NULL"
    ).fetchone()[0]
    print()
    print("  " + "-" * 74)
    if leaked == 0:
        print("  [OK] realized_slippage vazio em todas as amostras — correto na Fase 1.")
        print("       quoted_price_impact e quoted_drift NAO foram usados como substitutos.")
    else:
        print(f"  [ALERTA] {leaked} amostra(s) com realized_slippage preenchido na Fase 1.")
        print("           Isso não deveria acontecer fora da execução real.")

    fail_leaked = conn.execute(
        "SELECT COUNT(*) FROM round_trip WHERE tx_failure_rate IS NOT NULL"
    ).fetchone()[0]
    if fail_leaked == 0:
        print("  [OK] tx_failure_rate vazio em todas as amostras — correto na Fase 1.")
        print("       Sem execucao real nao ha falha para contar (ADR-001 §2).")
    else:
        print(f"  [ALERTA] {fail_leaked} amostra(s) com tx_failure_rate na Fase 1.")
        print("           Taxa de falha estimada sem execucao e numero inventado.")

    # Ao lado do .sqlite usado, para que um fixture nunca escreva em measurements/.
    out = out_dir / "day1_summary.json"
    out.write_text(
        json.dumps(
            {
                "summary": summary,
                "sol_usdc_price_measured": sol_price,
                "coverage": {
                    "windows": len(cov["windows"]),
                    "span_hours": cov["span_hours"],
                    "hours_of_day_utc": sorted(cov["hours_of_day"]),
                    "congestion_spread": cov["congestion_spread"],
                    "congestion_varied": cov["congestion_varied"],
                    "sufficient": cov["sufficient"],
                },
                "quoted_drift_bps_by_latency": {
                    str(h): dstats[h] for h in sorted(dstats)
                },
            },
            indent=2,
        ),
        "utf-8",
    )
    print(f"\n  Resumo em JSON: {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument(
        "--mints",
        type=int,
        default=DEFAULT_CORE_MINTS,
        help="mints distintos negociados = total de ATAs sob ADR-001 (carteira única)",
    )
    args = ap.parse_args()

    conn = ingest(args.jsonl, args.db)
    report(conn, core_mints=args.mints, out_dir=args.db.resolve().parent)
    conn.close()


if __name__ == "__main__":
    main()
