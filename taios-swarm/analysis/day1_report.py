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
    mid_drift_bps        REAL,   -- sonda de notional desprezível, pareada
    size_component_bps   REAL,   -- drift_bps - mid_drift_bps
    error                TEXT
);
"""

# Horizontes de latência usados na curva de custo.
BREAK_EVEN_HORIZONS_MS = [1000, 2000]

# Grade de tolerância de slippage (bps). slippageBps é parâmetro EVOLUÍDO por
# agente, não constante global — esta grade existe só para desenhar a curva e
# achar onde ela tem mínimo.
SLIPPAGE_GRID_BPS = [5, 10, 25, 50, 100, 200, 300, 500]

# Abaixo disto a cauda da distribuição de drift não tem resolução e a taxa de
# reversão calculada não significa nada.
MIN_DRIFT_SAMPLES = 30

# Posição central da tese — a curva é desenhada em detalhe para ela.
THESIS_POSITION_USDC = 1.0


def ingest(jsonl_path: Path, db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    # Bases criadas antes destas colunas continuam utilizáveis.
    for table, col, decl in [
        ("round_trip", "tx_failure_rate", "REAL"),
        ("quote_drift", "mid_drift_bps", "REAL"),
        ("quote_drift", "size_component_bps", "REAL"),
    ]:
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
                            elapsed_ms_request, elapsed_ms_response, drift_bps,
                            mid_drift_bps, size_component_bps, error)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            run_id,
                            size,
                            pt.get("nominalHorizonMs"),
                            pt.get("elapsedMsAtRequest"),
                            pt.get("elapsedMsAtResponse"),
                            pt.get("driftBps"),
                            pt.get("midDriftBps"),
                            pt.get("sizeComponentBps"),
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


def effective_n(conn, horizon_ms):
    """
    n bruto e n EFETIVO para estimativa de cauda.

    Observações de tamanhos dentro do mesmo bloco são contíguas no tempo, e
    volatilidade forma cluster — então elas não são independentes. A
    autocorrelação de lag 1 de |drift|, na ordem de medição, dá o desconto:

        n_eff = n × (1 − ρ) / (1 + ρ)

    Com ρ > 0 o n efetivo é menor que o bruto, e é o efetivo que manda na
    resolução de cauda.
    """
    vals = [
        r[0]
        for r in conn.execute(
            "SELECT drift_bps FROM quote_drift "
            "WHERE drift_bps IS NOT NULL AND nominal_horizon_ms = ? ORDER BY id",
            (horizon_ms,),
        )
    ]
    n = len(vals)
    if n < 3:
        return {"n": n, "rho": None, "n_eff": n}

    mags = [abs(v) for v in vals]  # cluster de volatilidade aparece na magnitude
    mean = sum(mags) / n
    dev = [m - mean for m in mags]
    denom = sum(d * d for d in dev)
    if denom == 0:
        return {"n": n, "rho": 0.0, "n_eff": n}

    rho = sum(dev[i] * dev[i + 1] for i in range(n - 1)) / denom
    rho = max(-0.99, min(0.99, rho))
    n_eff = n * (1 - rho) / (1 + rho)
    # Teto em n: rho negativo reduz variancia da media, mas nao cria
    # amostra nova para estimar cauda. Nunca reportar n_eff > n.
    return {"n": n, "rho": rho, "n_eff": max(1.0, min(float(n), n_eff))}


def calibration_section(conn):
    """
    Calibração quote_drift vs sonda de notional desprezível.

    A sonda é cotada em par com cada quote de tamanho real, a poucos ms de
    distância. A diferença entre os dois drifts isola o componente
    atribuível ao TAMANHO do que é movimento de preço puro.

    É este o papel do polling: n pequeno basta para calibrar uma relação.
    A cauda de drift adverso vem de histórico, no Day 2–3.
    """
    rows = list(
        conn.execute(
            """SELECT nominal_horizon_ms, position_size_usdc,
                      drift_bps, mid_drift_bps, size_component_bps
               FROM quote_drift
               WHERE drift_bps IS NOT NULL AND mid_drift_bps IS NOT NULL
               ORDER BY nominal_horizon_ms, position_size_usdc"""
        )
    )
    print()
    print("-" * 78)
    print("  CALIBRACAO: quote_drift vs sonda de notional despresivel")
    print("  (sonda cotada em par, a poucos ms — isola movimento de preco de tamanho)")
    print("-" * 78)
    if not rows:
        print("  Sem pares sonda/tamanho nesta base.")
        print("  Amostras anteriores a esta versao nao trazem a sonda — rode de novo.")
        return {}

    out = {}
    print()
    print(f"  {'HORIZ':>7} | {'TAM':>7} | {'n':>4} | {'|DRIFT|':>9} | {'|MID|':>9} | "
          f"{'COMP. TAM':>10} | {'RAZAO':>7}")
    print(f"  {'':>7} | {'US$':>7} | {'':>4} | {'bps med':>9} | {'bps med':>9} | "
          f"{'bps med':>10} | {'d/mid':>7}")
    print("  " + "-" * 68)
    by_key = {}
    for h, size, d, m, c in rows:
        by_key.setdefault((h, size), []).append((d, m, c))

    for (h, size), vals in sorted(by_key.items()):
        dm = statistics.median([abs(v[0]) for v in vals])
        mm = statistics.median([abs(v[1]) for v in vals])
        cm = statistics.median([abs(v[2]) for v in vals if v[2] is not None])
        ratio = (dm / mm) if mm else None
        print(
            f"  {str(h) + 'ms':>7} | {'$' + format(size, 'g'):>7} | {len(vals):>4} | "
            f"{dm:>9.2f} | {mm:>9.2f} | {cm:>10.2f} | "
            + (f"{ratio:>7.2f}" if ratio is not None else f"{'—':>7}")
        )
        out[f"{h}ms_{size}"] = {
            "horizon_ms": h,
            "position_usdc": size,
            "n": len(vals),
            "abs_drift_median_bps": dm,
            "abs_mid_drift_median_bps": mm,
            "abs_size_component_median_bps": cm,
            "ratio_drift_over_mid": ratio,
        }

    print()
    print("  Razao ~1 = o drift observado e movimento de preco, nao efeito de tamanho.")
    print("  Razao crescente com o tamanho = liquidez rasa mexendo a quote alem do preco.")
    return out


def signed_percentiles(conn):
    """
    Distribuição SINALIZADA de drift por horizonte, para a camada de evolução.

    Guardada com sinal de propósito: o custo do drift é condicional à direção
    do sinal do agente. Um agente de momentum e um de reversão à média veem o
    mesmo drift com sinais opostos. Colapsar em |x| aqui destruiria
    exatamente a informação que a evolução precisa.
    """
    by_h = {}
    for h, d in conn.execute(
        "SELECT nominal_horizon_ms, drift_bps FROM quote_drift "
        "WHERE drift_bps IS NOT NULL AND nominal_horizon_ms > 0"
    ):
        by_h.setdefault(h, []).append(d)
    return {
        str(h): {
            "n": len(v),
            "p05": pct(v, 0.05),
            "p25": pct(v, 0.25),
            "p50": pct(v, 0.50),
            "p75": pct(v, 0.75),
            "p95": pct(v, 0.95),
        }
        for h, v in sorted(by_h.items())
    }


def revert_floor(conn, horizon_ms, tolerances_bps=SLIPPAGE_GRID_BPS, n_eff=None):
    """
    Piso de taxa de reversão, derivado da distribuição de drift medida.

    A tolerância de slippage limita APENAS o lado adverso: se o preço anda a
    favor o swap captura o ganho, se anda contra além da tolerância a
    instrução falha e a tx reverte. Logo a probabilidade relevante é
    unilateral — P(drift < −tol) numa perna USDC->SOL — e não P(|drift| > tol).

    Pool entre tamanhos de posição: drift é movimento de preço, praticamente
    independente do tamanho. Sem pool não há amostra para resolver a cauda.

    Isto NÃO é txFailureRate: falha por congestionamento, blockhash expirado
    ou saldo reservado não devolvido vem de outros canais e não sai do drift.
    """
    vals = [
        r[0]
        for r in conn.execute(
            "SELECT drift_bps FROM quote_drift "
            "WHERE drift_bps IS NOT NULL AND nominal_horizon_ms = ?",
            (horizon_ms,),
        )
    ]
    n = len(vals)
    out = []
    for tol in tolerances_bps:
        if n == 0:
            out.append(
                {"tol_bps": tol, "p_leg": None, "p_round_trip": None, "n": 0, "upper_bound": False}
            )
            continue
        adverse = sum(1 for v in vals if v < -tol)
        if adverse == 0:
            # Teto pelo n EFETIVO: com observacoes correlacionadas a cauda
            # tem menos resolucao do que o n bruto sugere.
            p, upper = 1.0 / (n_eff or n), True  # teto, nao zero
        else:
            p, upper = adverse / n, False
        out.append(
            {
                "tol_bps": tol,
                "p_leg": p,
                "p_round_trip": 1 - (1 - p) ** 2,
                "n": n,
                "upper_bound": upper,
            }
        )
    return out


def curve_section(conn, summary, dstats):
    """
    Curva PRELIMINAR de custo por tolerância de slippage.

    Preliminar de propósito: o polling tem n pequeno e serve para calibrar a
    relação quote_drift vs sonda, não para resolver cauda. O cálculo
    definitivo de revert_rate_floor roda no Day 2–3 sobre histórico de trades,
    que tem ordens de magnitude mais amostras. Nada aqui bloqueia o Day 1.

    Braço direito: resolvido por ECONOMIA DO ATACANTE, não por medição de
    sandwich. Abaixo do piso econômico do MEV a curva é monotonicamente
    decrescente e o que se reporta é a menor tolerância que zera reversão.
    """
    out = {}
    thesis = next(
        (s for s in summary if abs(s["position_usdc"] - THESIS_POSITION_USDC) < 1e-9), None
    )
    if thesis is None:
        print()
        print(f"  Sem amostra de US${THESIS_POSITION_USDC:g} — curva não desenhada.")
        return out

    det_usdc = thesis["total_sunk_median_usdc"]
    net_usdc = thesis["network_median_usdc"]
    net_p90 = thesis["network_p90_usdc"]
    pos = thesis["position_usdc"]
    if det_usdc is None or net_usdc is None:
        print()
        print("  Custo determinístico incompleto — curva não desenhada.")
        return out

    # Custo do atacante: duas transações (front + back) em nível de corrida.
    # net_p90 é o total das DUAS pernas do nosso round trip, então uma tx
    # de swap em nível p90 ≈ net_p90/2.
    attacker_tx = (net_p90 / 2) if net_p90 else None
    attacker_cost = (2 * attacker_tx) if attacker_tx else None

    for horizon in BREAK_EVEN_HORIZONS_MS:
        eff = effective_n(conn, horizon)
        floors = revert_floor(conn, horizon, n_eff=eff["n_eff"])
        n, n_eff, rho = eff["n"], eff["n_eff"], eff["rho"]

        print()
        print("=" * 78)
        print(f"  CURVA PRELIMINAR — POSICAO US${pos:g}, LATENCIA {horizon}ms")
        print("=" * 78)
        print(f"  n bruto {n} | rho(lag1,|drift|) "
              + (f"{rho:.3f}" if rho is not None else "n/d")
              + f" | n EFETIVO {n_eff:.0f}")
        print(f"  Resolucao de cauda pelo n efetivo: {100/n_eff:.2f}%")
        print("  PRELIMINAR: revert_rate_floor definitivo sai no Day 2-3 sobre historico.")

        if n < MIN_DRIFT_SAMPLES:
            print()
            print(f"  n abaixo de {MIN_DRIFT_SAMPLES} — curva nao desenhada.")
            continue

        # ── Piso economico do MEV ──
        if attacker_cost:
            print()
            print("  PISO ECONOMICO DO MEV")
            print("  " + "-" * 68)
            print(f"    custo do atacante (2 tx, priority p90): ${attacker_cost:.6f}")
            max_safe_tol = attacker_cost * 10_000 / pos
            print(f"    TOLERANCIA MAXIMA que mantem US${pos:g} sob o piso: "
                  f"{max_safe_tol:.0f} bps")
            print(f"    Acima disso a extracao passa a cobrir o custo do atacante.")
            print()
            print(f"    {'TOL bps':>8} | {'EXTRACAO US$':>13} | {'LIMIAR POSICAO':>15} | {'US$' + format(pos, 'g'):>10}")
            print("    " + "-" * 60)
            for f in floors:
                tol = f["tol_bps"]
                extraction = pos * (tol / 10_000)
                threshold = attacker_cost * 10_000 / tol
                below = pos < threshold
                print(f"    {tol:>8} | {extraction:>13.6f} | {threshold:>15.2f} | "
                      f"{'SOB O PISO' if below else 'exposto':>10}")

        rows = []
        for f in floors:
            p = f["p_leg"]
            if p is None or p >= 1:
                continue
            wasted = net_usdc * (p / (1 - p))
            cost = det_usdc + wasted
            threshold = (attacker_cost * 10_000 / f["tol_bps"]) if attacker_cost else None
            below_floor = threshold is not None and pos < threshold
            sandwich = 0.0 if below_floor else pos * (f["tol_bps"] / 10_000)
            rows.append({
                "tol_bps": f["tol_bps"],
                "p_leg": p,
                "p_leg_is_upper_bound": f["upper_bound"],
                "p_round_trip": f["p_round_trip"],
                "wasted_gas_usdc": wasted,
                "break_even_pct": cost / pos * 100,
                "mev_threshold_position_usdc": threshold,
                "position_below_mev_floor": below_floor,
                "sandwich_applied_usdc": sandwich,
                "break_even_with_sandwich_pct": (cost + sandwich) / pos * 100,
            })

        print()
        print(f"  {'TOL':>5} | {'P(REVERT)':>10} | {'GAS DESP.':>10} | {'BE':>9} | {'MEV':>11}")
        print(f"  {'bps':>5} | {'por perna':>10} | {'US$':>10} | {'%':>9} | {'':>11}")
        print("  " + "-" * 58)
        for r in rows:
            mark = "<" if r["p_leg_is_upper_bound"] else " "
            mev = "sob o piso" if r["position_below_mev_floor"] else "exposto"
            print(f"  {r['tol_bps']:>5} | {r['p_leg']*100:>9.2f}%{mark} | "
                  f"{r['wasted_gas_usdc']:>10.6f} | {r['break_even_pct']:>8.3f}% | {mev:>11}")

        if any(r["p_leg_is_upper_bound"] for r in rows):
            print()
            print(f"  '<' = nenhuma amostra estourou a tolerancia; p e TETO de 1/n_eff,")
            print(f"        nao zero.")

        all_below = rows and all(r["position_below_mev_floor"] for r in rows)
        if all_below:
            zeroing = next((r for r in rows if r["p_leg_is_upper_bound"]), None)
            print()
            print(f"  US${pos:g} FICA SOB O PISO ECONOMICO DO MEV EM TODA A GRADE.")
            print("  Sanduichar essa posicao nao cobre o custo de duas transacoes do")
            print("  atacante mais a corrida de priority fee. O braco direito nao fecha,")
            print("  a curva e MONOTONICAMENTE DECRESCENTE e nao existe minimo interno.")
            print()
            if zeroing:
                print(f"  TOLERANCIA MINIMA QUE ZERA REVERSAO (ate a resolucao): "
                      f"{zeroing['tol_bps']} bps -> {zeroing['break_even_pct']:.3f}%")
            else:
                print("  Nenhuma tolerancia da grade zerou reversao na amostra.")
            print()
            print("  Esta e uma VANTAGEM ESTRUTURAL da microposicao, nao um achado")
            print("  de estrategia: o desenho opera abaixo do piso economico do MEV.")
        elif rows:
            best = min(rows, key=lambda r: r["break_even_with_sandwich_pct"])
            print()
            print(f"  MINIMO: {best['tol_bps']} bps -> {best['break_even_with_sandwich_pct']:.3f}%")

        out[f"{horizon}ms"] = {
            "rows": rows,
            "n_raw": n,
            "n_effective": n_eff,
            "rho_lag1": rho,
            "attacker_cost_usdc": attacker_cost,
            "max_safe_tolerance_bps": (attacker_cost * 10_000 / pos) if attacker_cost else None,
            "all_below_mev_floor": bool(all_below),
            "preliminary": True,
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
        print("  Mediana com sinal tende a zero — o preco anda para os dois lados.")
        print("  |drift| NAO entra como custo aditivo no break-even: o swap captura")
        print("  o lado favoravel integralmente. Somar o modulo seria penalidade de")
        print("  momentum aplicada tambem a agentes de reversao. O custo do drift e")
        print("  condicional a direcao do sinal e vive na camada de evolucao.")
        print("  O que e global e mensuravel aqui e a REVERSAO — tabela abaixo.")

    # ── Papel do polling: calibrar a relacao, nao resolver cauda ──
    calib_out = calibration_section(conn)

    # ── Curva PRELIMINAR; definitiva sai no Day 2-3 sobre historico ──
    curve_out = curve_section(conn, summary, dstats)

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
                # Distribuição SINALIZADA — a camada de evolução condiciona o
                # custo à direção do sinal de cada agente. Não colapsar em |x|.
                "quoted_drift_bps_by_latency": {
                    str(h): dstats[h] for h in sorted(dstats)
                },
                "quoted_drift_signed_percentiles": signed_percentiles(conn),
                # Piso de reversão vindo do drift. NÃO é txFailureRate.
                # PRELIMINAR: definitivo sai no Day 2-3 sobre histórico de trades.
                "revert_rate_floor_by_slippage_bps_PRELIMINARY": {
                    f"{h}ms": revert_floor(conn, h, n_eff=effective_n(conn, h)["n_eff"])
                    for h in BREAK_EVEN_HORIZONS_MS
                },
                "effective_n_by_horizon": {
                    f"{h}ms": effective_n(conn, h) for h in BREAK_EVEN_HORIZONS_MS
                },
                "quote_drift_vs_mid_calibration": calib_out,
                "break_even_curve_PRELIMINARY": curve_out,
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
