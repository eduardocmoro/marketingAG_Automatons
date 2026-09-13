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
    schema_version      INTEGER,
    git_sha             TEXT,
    fee_distinct_values INTEGER,
    fee_max_count       INTEGER,
    fee_top_values      TEXT,
    complete            INTEGER,
    rate_limit_hits     INTEGER,
    rent_matches_formula INTEGER,
    rent_formula_lamports INTEGER,
    cu_price_global_median REAL,
    cu_price_global_p90 REAL,
    rpc_url             TEXT,
    jupiter_host        TEXT,
    sol_usdc_price      REAL,
    base_fee_per_sig    INTEGER,
    cu_price_median     REAL,
    cu_price_p25        REAL,
    cu_price_p75        REAL,
    cu_price_p90        REAL,
    cu_price_p99        REAL,
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
    round_trip_return_pct   REAL,   -- <0 perda, >0 ganho
    leg_gap_ms              INTEGER,-- tempo entre as duas quotes
    fee_routeplan_usdc      REAL,   -- soma de feeAmount das duas pernas
    fee_routeplan_pct       REAL,
    fee_discrepancy_usdc    REAL,   -- observado - routePlan
    leg1_cu_consumed        INTEGER,
    leg2_cu_consumed        INTEGER,
    leg1_signatures         INTEGER,
    leg2_signatures         INTEGER,
    leg_gap_contaminated    INTEGER,-- gap entre pernas alto demais
    leg1_route_sig          TEXT,   -- pools+split concretos da perna 1
    leg2_route_sig          TEXT,
    error                   TEXT
);

CREATE TABLE IF NOT EXISTS route_hop (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             INTEGER NOT NULL REFERENCES run(run_id),
    position_size_usdc REAL NOT NULL,
    leg                INTEGER NOT NULL,
    hop_index          INTEGER NOT NULL,
    venue              TEXT,
    amm_key            TEXT,
    percent            REAL,
    fee_amount         TEXT,
    fee_mint           TEXT,
    fee_rate_bps       REAL,   -- tier do pool, direto do routePlan
    in_amount          TEXT,
    out_amount         TEXT
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
    route_match          INTEGER,-- sonda e quote passaram pela MESMA liquidez
    size_route           TEXT,
    probe_route          TEXT,
    size_route_changed   INTEGER,-- rota mudou entre t0 e t0+horizonte
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

# Piso físico de taxa: o tier CLMM mais barato que existe em pool relevante
# é 0,01% por swap = 0,02% no round trip. Perda medida abaixo disso significa
# que a taxa de pool NAO entrou na conta.
CHEAPEST_POOL_ROUND_TRIP_PCT = 0.02

# Gap maximo entre as duas quotes do round trip. Acima disto a deriva de
# preco compete com o custo medido e a amostra nao serve.
MAX_LEG_GAP_MS = 300


def ingest(jsonl_path: Path, db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    # Bases criadas antes destas colunas continuam utilizáveis.
    for table, col, decl in [
        ("round_trip", "tx_failure_rate", "REAL"),
        ("quote_drift", "mid_drift_bps", "REAL"),
        ("quote_drift", "size_component_bps", "REAL"),
        ("round_trip", "round_trip_return_pct", "REAL"),
        ("round_trip", "leg_gap_ms", "INTEGER"),
        ("round_trip", "fee_routeplan_usdc", "REAL"),
        ("round_trip", "fee_routeplan_pct", "REAL"),
        ("round_trip", "fee_discrepancy_usdc", "REAL"),
        ("round_trip", "leg1_route_sig", "TEXT"),
        ("round_trip", "leg2_route_sig", "TEXT"),
        ("run", "complete", "INTEGER"),
        ("run", "rate_limit_hits", "INTEGER"),
        ("run", "rent_matches_formula", "INTEGER"),
        ("run", "rent_formula_lamports", "INTEGER"),
        ("run", "cu_price_global_median", "REAL"),
        ("run", "cu_price_global_p90", "REAL"),
        ("run", "cu_price_p25", "REAL"),
        ("run", "cu_price_p75", "REAL"),
        ("run", "cu_price_p99", "REAL"),
        ("round_trip", "leg1_cu_consumed", "INTEGER"),
        ("round_trip", "leg2_cu_consumed", "INTEGER"),
        ("round_trip", "leg1_signatures", "INTEGER"),
        ("round_trip", "leg2_signatures", "INTEGER"),
        ("round_trip", "leg_gap_contaminated", "INTEGER"),
        ("run", "schema_version", "INTEGER"),
        ("run", "git_sha", "TEXT"),
        ("run", "fee_distinct_values", "INTEGER"),
        ("run", "fee_max_count", "INTEGER"),
        ("run", "fee_top_values", "TEXT"),
        ("quote_drift", "route_match", "INTEGER"),
        ("quote_drift", "size_route", "TEXT"),
        ("quote_drift", "probe_route", "TEXT"),
        ("quote_drift", "size_route_changed", "INTEGER"),
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
                    cu_price_samples, ata_rent_lamports,
                    complete, rate_limit_hits, rent_matches_formula,
                    rent_formula_lamports, cu_price_global_median,
                    cu_price_global_p90, cu_price_p25, cu_price_p75, cu_price_p99,
                    schema_version, git_sha, fee_distinct_values,
                    fee_max_count, fee_top_values)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    (rec.get("completeness") or {}).get("complete"),
                    len(rec.get("rateLimitEvents") or []),
                    (rec.get("ataRent") or {}).get("matchesFormula"),
                    (rec.get("ataRent") or {}).get("formulaLamports"),
                    ((rec.get("priorityFee") or {}).get("global") or {}).get("median"),
                    ((rec.get("priorityFee") or {}).get("global") or {}).get("p90"),
                    scoped.get("p25"),
                    scoped.get("p75"),
                    scoped.get("p99"),
                    rec.get("schemaVersion"),
                    rec.get("gitSha"),
                    scoped.get("distinctValues"),
                    scoped.get("maxCount"),
                    json.dumps(scoped.get("topValues")) if scoped.get("topValues") else None,
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
                        cu_consumed_source, tx_failure_rate,
                        round_trip_return_pct, leg_gap_ms,
                        fee_routeplan_usdc, fee_routeplan_pct,
                        fee_discrepancy_usdc, leg1_route_sig, leg2_route_sig,
                        leg1_cu_consumed, leg2_cu_consumed,
                        leg1_signatures, leg2_signatures,
                        leg_gap_contaminated, error)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                        rt.get("roundTripReturnPct"),
                        rt.get("legGapMs"),
                        (rt.get("feeCrossCheck") or {}).get("totalFeeUsdc"),
                        (rt.get("feeCrossCheck") or {}).get("totalFeePctOfPosition"),
                        (rt.get("feeCrossCheck") or {}).get("discrepancyUsdc"),
                        (leg1.get("route") or {}).get("signature"),
                        (leg2.get("route") or {}).get("signature"),
                        (leg1.get("compute") or {}).get("cuConsumed"),
                        (leg2.get("compute") or {}).get("cuConsumed"),
                        (leg1.get("compute") or {}).get("signatures"),
                        (leg2.get("compute") or {}).get("signatures"),
                        None if rt.get("legGapContaminated") is None
                        else int(rt["legGapContaminated"]),
                        rt.get("error"),
                    ),
                )

                for leg_no, leg in ((1, leg1), (2, leg2)):
                    for i, hop in enumerate((leg.get("route") or {}).get("legs", [])):
                        conn.execute(
                            """INSERT INTO route_hop
                               (run_id, position_size_usdc, leg, hop_index, venue,
                                amm_key, percent, fee_amount, fee_mint,
                                fee_rate_bps, in_amount, out_amount)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                run_id,
                                rt.get("positionSizeUsdc"),
                                leg_no,
                                i,
                                hop.get("venue"),
                                hop.get("ammKey"),
                                hop.get("percent"),
                                hop.get("feeAmount"),
                                hop.get("feeMint"),
                                hop.get("feeRateBps"),
                                hop.get("inAmount"),
                                hop.get("outAmount"),
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
                            mid_drift_bps, size_component_bps,
                            route_match, size_route, probe_route,
                            size_route_changed, error)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            run_id,
                            size,
                            pt.get("nominalHorizonMs"),
                            pt.get("elapsedMsAtRequest"),
                            pt.get("elapsedMsAtResponse"),
                            pt.get("driftBps"),
                            pt.get("midDriftBps"),
                            pt.get("sizeComponentBps"),
                            None if pt.get("routeMatch") is None else int(pt["routeMatch"]),
                            pt.get("sizeRoute"),
                            pt.get("probeRoute"),
                            None if pt.get("sizeRouteChangedFromBase") is None
                            else int(pt["sizeRouteChangedFromBase"]),
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


def build_cohort(conn):
    """
    Segmenta por versao do codigo e expoe so a mais recente para analise.

    O jsonl e append-only e acumula versoes do script ao longo dos dias. A
    correcao do gap entre pernas (schema v3) mudou o que o round trip mede —
    agregar v2 e v3 junto faz dado velho envenenar dado novo. Amostras sem
    schema_version sao tratadas como versao 0 (pre-instrumentacao).
    """
    versions = conn.execute(
        "SELECT COALESCE(schema_version, 0) AS v, COUNT(*), "
        "       GROUP_CONCAT(DISTINCT COALESCE(git_sha,'?')) "
        "FROM run GROUP BY v ORDER BY v DESC"
    ).fetchall()
    latest = versions[0][0] if versions else 0

    for view, table in [("round_trip_cohort", "round_trip"),
                        ("quote_drift_cohort", "quote_drift"),
                        ("route_hop_cohort", "route_hop")]:
        conn.execute(f"DROP VIEW IF EXISTS {view}")
        conn.execute(
            f"CREATE TEMP VIEW {view} AS SELECT * FROM {table} WHERE run_id IN "
            f"(SELECT run_id FROM run WHERE COALESCE(schema_version,0) = {latest})"
        )

    total_rt = conn.execute("SELECT COUNT(*) FROM round_trip").fetchone()[0]
    kept_rt = conn.execute("SELECT COUNT(*) FROM round_trip_cohort").fetchone()[0]
    total_qd = conn.execute("SELECT COUNT(*) FROM quote_drift").fetchone()[0]
    kept_qd = conn.execute("SELECT COUNT(*) FROM quote_drift_cohort").fetchone()[0]

    print()
    print("=" * 78)
    print("  COORTE DE ANALISE — SEGMENTADA POR VERSAO DO CODIGO")
    print("=" * 78)
    print()
    print(f"    {'SCHEMA':>7} | {'EXECUCOES':>10} | {'':>4} | git sha")
    print("    " + "-" * 60)
    for v, n, shas in versions:
        mark = "USA" if v == latest else "excl"
        print(f"    {('v' + str(v)) if v else 'pre':>7} | {n:>10} | {mark:>4} | {shas or '?'}")
    print()
    print(f"    round trip : {kept_rt}/{total_rt} amostras na coorte "
          f"({total_rt - kept_rt} excluidas)")
    print(f"    drift      : {kept_qd}/{total_qd} amostras na coorte "
          f"({total_qd - kept_qd} excluidas)")
    if total_rt - kept_rt > 0:
        print()
        print("    Versoes anteriores medem coisa diferente e NAO entram nas tabelas")
        print("    de custo, TESTE 0 e TESTE 4. Continuam no jsonl como historico.")
    return {"latest_schema": latest,
            "versions": [{"schema": v, "runs": n, "git_sha": shas} for v, n, shas in versions],
            "round_trip_kept": kept_rt, "round_trip_total": total_rt,
            "drift_kept": kept_qd, "drift_total": total_qd}


def integrity_section(conn):
    """
    Checagem de integridade da medição, ANTES de qualquer leitura de custo.

    Três testes que um resultado válido tem que passar:
      1. Monotonicidade — price impact cresce com o tamanho. Se US$500 sai
         melhor que US$0,50 no mesmo pool, a medição está errada.
      2. Piso físico — perda de round trip abaixo de 0,02% significa que a
         taxa de pool não entrou na conta.
      3. Validação cruzada — a soma de feeAmount do routePlan das duas pernas
         tem que explicar a perda observada. Caminho independente do
         encadeamento de quotes: se não bater, o bug é do nosso cálculo.
    """
    print()
    print("=" * 78)
    print("  INTEGRIDADE DA MEDICAO")
    print("=" * 78)

    verdicts = {"leg_gap": None, "monotonic": None, "above_floor": None,
                "fee_crosscheck": None, "signal_vs_drift": None}

    # ── Completude e rate limit ──
    incomplete = conn.execute(
        "SELECT COUNT(*) FROM run WHERE complete = 0"
    ).fetchone()[0]
    hits = conn.execute(
        "SELECT COALESCE(SUM(rate_limit_hits),0) FROM run"
    ).fetchone()[0]
    total_runs = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
    if incomplete or hits:
        print()
        print(f"  [!] {incomplete}/{total_runs} execucao(oes) incompleta(s), "
              f"{hits} bloqueio(s) 429 da Jupiter.")
        print("      Suba o espacamento: JUP_MIN_GAP_MS=2000")

    # ── Rent: medido vs formula ──
    for measured, formula, matches in conn.execute(
        "SELECT ata_rent_lamports, rent_formula_lamports, rent_matches_formula "
        "FROM run WHERE ata_rent_lamports IS NOT NULL LIMIT 1"
    ):
        print()
        print("  RENT DE ATA")
        print(f"    medido  : {measured:,} lamports")
        if formula:
            print(f"    formula : {formula:,} lamports  ((128+165) x 2540 x 2)")
            if matches == 0:
                print(f"    [!] DIVERGE em {abs(measured-formula):,} lamports "
                      f"({(measured/formula-1)*100:+.1f}%)")
                print("        O relatorio usa o MEDIDO. Ver sondas de 0 e 82 bytes no")
                print("        jsonl para diagnosticar o schedule de rent do RPC.")

    # ── Priority fee: global vs filtrado por pool ──
    for gm, gp, fm, fp in conn.execute(
        "SELECT cu_price_global_median, cu_price_global_p90, "
        "cu_price_median, cu_price_p90 FROM run "
        "WHERE cu_price_median IS NOT NULL LIMIT 1"
    ):
        print()
        print("  PRIORITY FEE (micro-lamports/CU)")
        print(f"    global (rede inteira) : mediana {gm} | p90 {gp}")
        print(f"    filtrado por pool     : mediana {fm} | p90 {fp}")
        if gm and gp and gm > 0 and gp / gm > 50:
            print(f"    [!] spread global de {gp/gm:.0f}x — o global e ruido da rede,")
            print("        nao a taxa relevante. Use a linha filtrada por pool.")

    # ── Agrupamento por rota: pre-requisito para comparar tamanhos ──
    groups = {}
    for size, sig1, sig2, h1, h2 in conn.execute(
        "SELECT position_size_usdc, leg1_route_sig, leg2_route_sig, leg1_hops, leg2_hops "
        "FROM round_trip_cohort WHERE error IS NULL "
        "GROUP BY position_size_usdc ORDER BY position_size_usdc"
    ):
        groups.setdefault((sig1, sig2), []).append((size, h1, h2))

    print()
    print("  AGRUPAMENTO POR ROTA")
    print("  " + "-" * 68)
    if not groups:
        print("    sem dados de rota")
    else:
        for i, ((sig1, sig2), members) in enumerate(groups.items(), 1):
            sizes = ", ".join("$" + format(m[0], "g") for m in members)
            hops = members[0]
            print(f"    grupo {i}: hops {hops[1]}+{hops[2]}  ->  {sizes}")
        if len(groups) > 1:
            print()
            print(f"    {len(groups)} rotas distintas entre os tamanhos. Comparar perda")
            print("    entre grupos mistura taxa de venue com impacto de tamanho.")

    # ── Item 3: proveniencia do cu_consumed ──
    print()
    print("  PROVENIENCIA DO cu_consumed")
    print("  " + "-" * 68)
    print("    Toda a tabela de break-even depende deste numero.")
    sources = conn.execute(
        "SELECT COALESCE(cu_consumed_source,'?'), COUNT(*), AVG(leg1_cu_consumed) "
        "FROM round_trip_cohort WHERE error IS NULL GROUP BY cu_consumed_source"
    ).fetchall()
    cu_by_source = {}
    if not sources:
        print("    sem dados")
    else:
        tot = sum(n for _, n, _ in sources)
        print()
        print(f"    {'FONTE':>20} | {'AMOSTRAS':>9} | {'%':>6} | {'cu medio':>10}")
        print("    " + "-" * 56)
        for src, n, avg_cu in sources:
            print(f"    {src:>20} | {n:>9} | {n/tot*100:>5.1f}% | "
                  + (f"{avg_cu:>10.0f}" if avg_cu else f"{'—':>10}"))
            cu_by_source[src] = {"samples": n, "avg_cu": avg_cu}
        sim = cu_by_source.get("simulation", {}).get("avg_cu")
        est = cu_by_source.get("jupiter_estimate", {}).get("avg_cu")
        if sim and est:
            div = abs(sim - est) / min(sim, est) * 100
            print()
            print(f"    simulacao {sim:.0f} vs estimativa Jupiter {est:.0f} "
                  f"-> divergencia {div:.1f}%")
            if div > 10:
                print("    [!] As duas fontes discordam. O break-even muda conforme a fonte.")
        elif est and not sim:
            print()
            print("    [!] NENHUMA amostra veio de simulateTransaction — tudo e")
            print("        estimativa do Jupiter. O RPC publico recusa simulacao.")
            print("        Use SOLANA_RPC_URL dedicado para medir cu de verdade.")

    # ── Item 4: saturacao na coleta de priority fee ──
    print()
    print("  FORMA DA COLETA DE PRIORITY FEE (deteccao de cap)")
    print("  " + "-" * 68)
    shape = conn.execute(
        "SELECT cu_price_samples, fee_distinct_values, fee_max_count, fee_top_values, "
        "       cu_price_p90, cu_price_p99 FROM run "
        "WHERE fee_top_values IS NOT NULL ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if not shape:
        print("    Sem dados de forma — amostra de versao anterior do script.")
        print("    Percentil redondo (ex.: 1.500.000 exato) e suspeito de teto e nao")
        print("    de observacao; a versao nova grava os valores de topo para checar.")
    else:
        n_s, distinct, max_count, top_json, p90, p99 = shape
        print(f"    amostras {n_s} | valores distintos {distinct} | "
              f"empates no maximo {max_count}")
        try:
            top = json.loads(top_json)
            print()
            print(f"    {'VALOR':>14} | {'OCORRENCIAS':>12}")
            print("    " + "-" * 30)
            for t in top:
                print(f"    {t['value']:>14,} | {t['count']:>12}")
        except (json.JSONDecodeError, TypeError, KeyError):
            top = []
        if n_s and max_count and max_count / n_s > 0.1:
            print()
            print(f"    [!] {max_count/n_s*100:.0f}% das amostras empatam no valor maximo.")
            print("        Isso e assinatura de CAP ou saturacao, nao de distribuicao")
            print("        empirica. Percentis altos ficam suspeitos.")
        for label, v in [("p90", p90), ("p99", p99)]:
            if v and v >= 100000 and v % 100000 == 0:
                print()
                print(f"    [!] {label} = {v:,.0f} e redondo demais para ser empirico.")
                print("        Verifique se ha teto na coleta antes de usar no break-even.")

    # ── Teste 1 (SECUNDARIO): monotonicidade DENTRO da mesma rota ──
    rows = conn.execute(
        "SELECT position_size_usdc, AVG(swap_loss_pct), COUNT(*) "
        "FROM round_trip_cohort WHERE error IS NULL AND swap_loss_pct IS NOT NULL "
        "GROUP BY position_size_usdc ORDER BY position_size_usdc"
    ).fetchall()
    loss_by_size = {r[0]: r[1] for r in rows}

    print()
    print("  TESTE 1 (secundario) — MONOTONICIDADE DENTRO DA MESMA ROTA")
    print("  " + "-" * 68)
    print("    So vale entre tamanhos que atravessaram a MESMA liquidez.")
    comparable = [g for g in groups.values() if len(g) >= 2]
    if not comparable:
        print("    Nenhum grupo de rota tem 2+ tamanhos — teste NAO APLICAVEL.")
        print("    Nao-monotonicidade entre rotas diferentes nao e evidencia de bug.")
        verdicts["monotonic"] = None
    else:
        violations = []
        for members in comparable:
            ordered = sorted(members, key=lambda m: m[0])
            prev = None
            for size, _, _ in ordered:
                loss = loss_by_size.get(size)
                if loss is None:
                    continue
                flag = ""
                if prev is not None and loss < prev[1] - 1e-9:
                    flag = f"  <-- MENOR que ${prev[0]:g} na mesma rota"
                    violations.append((prev[0], size))
                print(f"    {'$' + format(size, 'g'):>9} | {loss:>12.4f}%{flag}")
                prev = (size, loss)
        verdicts["monotonic"] = not violations
        print()
        print("    [FALHOU] impacto caiu com o tamanho na mesma rota."
              if violations else "    [OK] monotonico dentro de cada rota.")

    # ── Teste 2: piso fisico ──
    print()
    print(f"  TESTE 2 — PISO FISICO (perda >= {CHEAPEST_POOL_ROUND_TRIP_PCT}% no round trip)")
    print("  " + "-" * 68)
    below = [(sz, l) for sz, l, _ in rows if l < CHEAPEST_POOL_ROUND_TRIP_PCT]
    if not rows:
        print("    sem amostras")
    elif below:
        verdicts["above_floor"] = False
        print(f"    [ATENCAO] {len(below)}/{len(rows)} tamanhos abaixo de "
              f"{CHEAPEST_POOL_ROUND_TRIP_PCT}%.")
        for sz, l in below[:8]:
            print(f"      ${sz:g}: {l:.4f}%")
        print("      Se a taxa efetiva medida (tabela abaixo) confirmar tier baixo,")
        print("      isto NAO e bug — e venue barato. O TESTE 3 decide.")
    else:
        verdicts["above_floor"] = True
        print("    [OK] todos os tamanhos acima do piso.")

    # ── TESTE 0: contaminacao por gap entre pernas ──
    # Contaminacao DERIVADA do proprio gap, nao do campo gravado pelo script:
    # amostras de versoes anteriores nao tem esse campo e um COALESCE(...,0)
    # as contava como limpas, produzindo contagem incoerente com a media.
    gaps = [r[0] for r in conn.execute(
        "SELECT leg_gap_ms FROM round_trip_cohort "
        "WHERE error IS NULL AND leg_gap_ms IS NOT NULL ORDER BY leg_gap_ms"
    )]
    print()
    print("  TESTE 0 — GAP ENTRE AS PERNAS DO ROUND TRIP")
    print("  " + "-" * 68)
    if not gaps:
        print("    sem dados de gap")
    else:
        total = len(gaps)
        contaminated = sum(1 for g in gaps if g > MAX_LEG_GAP_MS)
        avg_gap, max_gap = sum(gaps) / total, gaps[-1]
        print(f"    n={total} | p50 {pct(gaps,0.5):.0f}ms | p90 {pct(gaps,0.9):.0f}ms | "
              f"max {max_gap:.0f}ms | media {avg_gap:.0f}ms")
        print(f"    limite {MAX_LEG_GAP_MS}ms -> {contaminated} acima ({contaminated/total*100:.0f}%)")
        if contaminated:
            verdicts["leg_gap"] = False
            print(f"    [FALHOU] {contaminated}/{total} amostras acima do limite.")
            print("             A deriva de preco entre as pernas compete com o custo")
            print("             medido. Essas amostras medem oscilacao, nao custo.")
        else:
            verdicts["leg_gap"] = True
            print(f"    [OK] {total} amostras dentro do limite.")

    # ── Classificacao de venue: AMM com fee declarada vs venue de spread ──
    print()
    print("  CLASSIFICACAO DE VENUE")
    print("  " + "-" * 68)
    venues = conn.execute(
        "SELECT venue, COUNT(*), SUM(CASE WHEN fee_rate_bps > 0 THEN 1 ELSE 0 END), "
        "       AVG(CASE WHEN fee_rate_bps > 0 THEN fee_rate_bps END) "
        "FROM route_hop_cohort GROUP BY venue ORDER BY COUNT(*) DESC"
    ).fetchall()
    declared = spread = 0
    venue_rows = []
    if not venues:
        print("    sem dados de rota")
    else:
        print(f"    {'VENUE':>16} | {'ROTAS':>6} | {'COM FEE':>8} | {'TIER bps':>9} | TIPO")
        print("    " + "-" * 62)
        for venue, n, with_fee, avg_tier in venues:
            with_fee = with_fee or 0
            kind = "AMM (fee declarada)" if with_fee > 0 else "spread (fee embutida)"
            declared += with_fee
            spread += n - with_fee
            tier = f"{avg_tier:.2f}" if avg_tier else "—"
            print(f"    {(venue or '?')[:16]:>16} | {n:>6} | {with_fee:>8} | {tier:>9} | {kind}")
            venue_rows.append({"venue": venue, "routes": n, "with_fee": with_fee,
                               "avg_tier_bps": avg_tier})
        total_hops = declared + spread
        pct_declared = declared / total_hops * 100 if total_hops else 0
        print()
        print(f"    saltos com feeAmount declarada: {declared}/{total_hops} "
              f"({pct_declared:.1f}%)")
        if pct_declared < 50:
            print()
            print("    A maioria das rotas passa por VENUE DE SPREAD (market maker /")
            print("    cotacao privada), que nao declara feeAmount — o custo esta")
            print("    embutido no preco cotado. Para esses, a perda do round trip E a")
            print("    medicao; nao existe fee para validar contra.")

    # ── Teste 3: validacao cruzada, SO onde ha fee declarada ──
    print()
    print("  TESTE 3 — PERDA OBSERVADA vs feeAmount DECLARADA")
    print("  " + "-" * 68)
    cross = conn.execute(
        "SELECT position_size_usdc, AVG(swap_loss_pct), AVG(fee_routeplan_pct), "
        "       AVG(fee_discrepancy_usdc), AVG(leg_gap_ms), COUNT(*) "
        "FROM round_trip_cohort WHERE error IS NULL AND fee_routeplan_pct > 0 "
        "GROUP BY position_size_usdc ORDER BY position_size_usdc"
    ).fetchall()
    if not cross:
        verdicts["fee_crosscheck"] = None
        print("    NAO APLICAVEL: nenhuma rota declarou feeAmount > 0.")
        print("    Venue de spread embute o custo no preco e nao expoe taxa.")
        print("    Comparar contra zero nao valida nada — o teste fica suspenso,")
        print("    nao aprovado nem reprovado.")
        print()
        print("    VALIDADOR NECESSARIO (nao implementado): comparar outAmount")
        print("    contra preco de referencia INDEPENDENTE da Jupiter — oracle")
        print("    on-chain (Pyth). Comparar quote contra quote da mesma fonte e")
        print("    circular. Ver README, secao 'Validador para venue de spread'.")
    else:
        print()
        print(f"    {'TAM':>7} | {'OBSERVADO %':>12} | {'FEE DECL. %':>12} | "
              f"{'DIFF US$':>11} | {'gap ms':>7}")
        bad = 0
        for size, obs, fee, disc, gap, n in cross:
            suspect = obs < fee - 1e-9
            if suspect:
                bad += 1
            print(f"    {'$' + format(size, 'g'):>7} | {obs:>12.4f} | {fee:>12.4f} | "
                  f"{disc:>11.6f} | {gap or 0:>7.0f}" + ("  <-- ABAIXO da taxa" if suspect else ""))
        verdicts["fee_crosscheck"] = bad == 0
        print()
        print(f"    [FALHOU] {bad} tamanho(s) com perda menor que a fee declarada."
              if bad else "    [OK] perda cobre a fee declarada onde ela existe.")

    # ── TESTE 4: sinal vs ruido de deriva ──
    print()
    print("  TESTE 4 — PERDA MEDIDA vs DERIVA NO MESMO INTERVALO")
    print("  " + "-" * 68)
    print("    Validador que funciona tambem em venue de spread: a perda so e")
    print("    custo se for grande perto da oscilacao de preco no mesmo gap.")
    drift_by_h = drift_stats(conn)
    rt_rows = conn.execute(
        "SELECT position_size_usdc, AVG(swap_loss_pct), AVG(leg_gap_ms), COUNT(*) "
        "FROM round_trip_cohort WHERE error IS NULL AND swap_loss_pct IS NOT NULL "
        "AND leg_gap_ms IS NOT NULL GROUP BY position_size_usdc ORDER BY position_size_usdc"
    ).fetchall()
    if not drift_by_h or not rt_rows:
        verdicts["signal_vs_drift"] = None
        print("    sem dados de drift ou de gap — teste nao aplicavel.")
    else:
        print()
        print(f"    {'TAM':>7} | {'PERDA bps':>10} | {'gap ms':>7} | "
              f"{'|DRIFT| bps':>12} | {'RAZAO':>7}")
        weak = 0
        for size, loss_pct, gap, n in rt_rows:
            horizon = min(drift_by_h, key=lambda h: abs(h - (gap or 0)))
            d = drift_by_h[horizon]["median_abs"]
            loss_bps = loss_pct * 100
            ratio = (loss_bps / d) if d and d > 0 else None
            if ratio is not None and ratio < 3:
                weak += 1
            print(f"    {'$' + format(size, 'g'):>7} | {loss_bps:>10.3f} | {gap or 0:>7.0f} | "
                  f"{d:>12.3f} | " + (f"{ratio:>6.1f}x" if ratio else f"{'—':>7}")
                  + ("  <-- indistinguivel de ruido" if ratio and ratio < 3 else ""))
        verdicts["signal_vs_drift"] = weak == 0
        print()
        if weak:
            print(f"    [FALHOU] {weak} tamanho(s) com perda da mesma ordem da deriva.")
            print("             Nao da para separar custo de oscilacao de preco.")
            print("             Reduza o gap entre as pernas antes de ler custo.")
        else:
            print("    [OK] perda pelo menos 3x a deriva do mesmo intervalo.")

    # ── Taxa efetiva de pool por tamanho: RESULTADO, nao premissa ──
    print()
    print("  TAXA EFETIVA DE POOL POR TAMANHO — RESULTADO DO DAY 1")
    print("  " + "-" * 68)
    print("    Tier lido do routePlan (feeAmount/base do salto). Nao e premissa")
    print("    herdada: 0,25% do Raydium nunca foi medido, foi assumido.")
    hops = conn.execute(
        "SELECT position_size_usdc, leg, hop_index, venue, AVG(fee_rate_bps), COUNT(*) "
        "FROM route_hop_cohort WHERE fee_rate_bps IS NOT NULL "
        "GROUP BY position_size_usdc, leg, hop_index, venue "
        "ORDER BY position_size_usdc, leg, hop_index"
    ).fetchall()
    fee_table = []
    if not hops:
        print()
        print("    Sem feeRateBps — amostra de versao anterior do script.")
    else:
        print()
        print(f"    {'TAM':>7} | {'PERNA':>5} | {'HOP':>3} | {'VENUE':>18} | {'TIER bps':>9}")
        for size, leg, hop, venue, rate, n in hops:
            print(f"    {'$' + format(size, 'g'):>7} | {leg:>5} | {hop:>3} | "
                  f"{(venue or '?')[:18]:>18} | {rate:>9.2f}")
            fee_table.append({"position_usdc": size, "leg": leg, "hop": hop,
                              "venue": venue, "fee_rate_bps": rate, "n": n})
        tiers = [h[4] for h in hops]
        print()
        print(f"    tier minimo observado: {min(tiers):.2f} bps | maximo: {max(tiers):.2f} bps")
        if min(tiers) < 25:
            print()
            print("    [!] Ha salto com tier ABAIXO de 25 bps (0,25%). A premissa de")
            print("        Raydium 0,25% que carregamos a conversa inteira esta ERRADA")
            print("        PARA CIMA nesses caminhos. Isso FAVORECE o desenho de US$1 —")
            print("        e e resultado medido, nao suposicao.")

    # ── Veredito ──
    print()
    print("  " + "=" * 68)
    if verdicts["leg_gap"] is False:
        print("  VEREDITO: MEDICAO INVALIDA — as pernas do round trip sairam")
        print("  separadas demais. O que foi medido e deriva de preco, nao custo.")
        print("  Corrija o gap e remeça antes de ler qualquer numero.")
    elif verdicts["fee_crosscheck"] is False:
        print("  VEREDITO: MEDICAO INVALIDA — perda menor que a fee declarada.")
    elif verdicts["signal_vs_drift"] is False:
        print("  VEREDITO: MEDICAO INCONCLUSIVA — a perda tem a mesma ordem de")
        print("  grandeza da deriva no mesmo intervalo. Nao da para separar custo")
        print("  de oscilacao. Nao use os numeros abaixo.")
    elif verdicts["fee_crosscheck"] is True or verdicts["signal_vs_drift"] is True:
        if verdicts["fee_crosscheck"] is True:
            print("  VEREDITO: validado contra fee declarada e contra deriva.")
        else:
            print("  VEREDITO: validado contra DERIVA apenas.")
            print("  A rota passa por venue de spread, que nao declara fee — falta o")
            print("  validador independente (oracle Pyth). Tratar como PARCIAL.")
        if verdicts["monotonic"] is False:
            print("  Ressalva: monotonicidade violada dentro de uma mesma rota.")
        if verdicts["above_floor"] is False:
            print("  Perda abaixo de 0,02% e esperada em venue de spread barato.")
    else:
        print("  VEREDITO: sem dados suficientes para validar.")
    print("  " + "=" * 68)
    return {**verdicts, "route_groups": len(groups), "pool_fee_by_size": fee_table}


def decomposition_section(conn, sol_price):
    """
    Custo de rede é FIXO em dólar; taxa de pool é PERCENTUAL.

    Como % da posição, a rede CRESCE quando a posição encolhe e o pool fica
    constante. Descobrir tier baixo reduz o componente que já era menor em
    posição pequena — não salva o desenho.

    Emitido em DUAS versões: rede na mediana e rede no p90 da priority fee.
    A versão p90 descreve o pior caso operacional e é ela que decide o
    desenho — deixar só a mediana visível subestima o custo por ~8x.

        rede_pct(tam) = rede_usd / tam × 100
        pool_pct      = constante
        cruzamento    = rede_usd × 100 / pool_pct
    """
    rows = conn.execute(
        "SELECT position_size_usdc, AVG(net_lamports_median), AVG(net_lamports_p90), "
        "       AVG(fee_routeplan_pct), COUNT(*) "
        "FROM round_trip_cohort WHERE error IS NULL AND net_lamports_median IS NOT NULL "
        "GROUP BY position_size_usdc ORDER BY position_size_usdc"
    ).fetchall()
    print()
    print("=" * 78)
    print("  DECOMPOSICAO: REDE (FIXA) vs POOL (PERCENTUAL)")
    print("=" * 78)
    if not rows or sol_price is None:
        print("  sem dados suficientes")
        return {}

    print()
    print(f"  {'':>8} {'':>9}   {'--- REDE NA MEDIANA ---':^24}  {'--- REDE NO p90 ---':^24}")
    print(f"  {'TAMANHO':>8} {'POOL %':>9} | {'US$':>9} {'% POS':>8} {'DOMIN':>10} | "
          f"{'US$':>9} {'% POS':>8} {'DOMIN':>10}")
    print("  " + "-" * 74)

    out, net_med_usds, net_p90_usds, pool_pcts = [], [], [], []
    for size, net_med, net_p90, pool_pct, n in rows:
        nm = net_med / LAMPORTS_PER_SOL * sol_price
        np90 = (net_p90 / LAMPORTS_PER_SOL * sol_price) if net_p90 else None
        nm_pct = nm / size * 100
        np_pct = (np90 / size * 100) if np90 else None
        net_med_usds.append(nm)
        if np90:
            net_p90_usds.append(np90)
        if pool_pct is not None:
            pool_pcts.append(pool_pct)

        def dom(net_pct):
            if pool_pct is None or pool_pct <= 0 or net_pct is None:
                return "—"
            r = net_pct / pool_pct
            return f"rede {r:.1f}x" if r >= 1 else f"pool {1/r:.1f}x"

        pool_s = "—" if pool_pct is None else f"{pool_pct:.4f}"
        p90_cells = (f"{np90:>9.6f} {np_pct:>7.4f}% {dom(np_pct):>10}"
                     if np90 else f"{'—':>9} {'—':>8} {'—':>10}")
        print(f"  {'$' + format(size, 'g'):>8} {pool_s:>9} | "
              f"{nm:>9.6f} {nm_pct:>7.4f}% {dom(nm_pct):>10} | {p90_cells}")
        out.append({"position_usdc": size, "pool_pct": pool_pct,
                    "network_median_usd": nm, "network_median_pct": nm_pct,
                    "network_p90_usd": np90, "network_p90_pct": np_pct})

    # ── Cruzamento nas duas versoes ──
    cross_out = {}
    if net_med_usds and pool_pcts:
        ref_med = statistics.median(net_med_usds)
        ref_p90 = statistics.median(net_p90_usds) if net_p90_usds else None
        print()
        print("  CRUZAMENTO — tamanho minimo economicamente racional")
        print("  " + "-" * 74)
        print(f"    rede por round trip: mediana ${ref_med:.6f}"
              + (f" | p90 ${ref_p90:.6f}" if ref_p90 else ""))
        print()
        print(f"    {'TIER POOL':>11} | {'CRUZAMENTO med':>15} | {'CRUZAMENTO p90':>15}")
        print("    " + "-" * 50)
        for pool_pct in sorted(set(round(x, 4) for x in pool_pcts)):
            if pool_pct <= 0:
                continue
            cm = ref_med * 100 / pool_pct
            cp = (ref_p90 * 100 / pool_pct) if ref_p90 else None
            print(f"    {pool_pct:>10.4f}% | {'$' + format(round(cm, 2), 'g'):>15} | "
                  + (f"{'$' + format(round(cp, 2), 'g'):>15}" if cp else f"{'—':>15}"))
            cross_out[str(round(pool_pct, 4))] = {"median": cm, "p90": cp}
        print()
        print("    Abaixo do cruzamento a REDE domina — taxa que nao diminui por mais")
        print("    que a ordem encolha. Tier MENOR empurra o cruzamento para CIMA.")
        if ref_p90:
            print()
            print("    A coluna p90 e a que descreve o pior caso operacional. Um desenho")
            print("    que so fecha na coluna mediana nao fecha em congestionamento.")
    return {"by_size": out, "crossover_usdc": cross_out}


def dispersion_section(conn, sol_price):
    """
    Dispersão da priority fee FILTRADA por pool.

    Mais decisivo que o tier: se a cauda persistir depois do filtro por
    ammKey, o custo em US$1 varia de forma imprevisível e nenhum alvo fixo
    de lucro por trade se sustenta.
    """
    print()
    print("=" * 78)
    print("  DISPERSAO DA PRIORITY FEE FILTRADA POR POOL")
    print("=" * 78)
    row = conn.execute(
        "SELECT cu_price_p25, cu_price_median, cu_price_p75, cu_price_p90, cu_price_p99, "
        "       cu_price_global_median, cu_price_global_p90 "
        "FROM run WHERE cu_price_median IS NOT NULL ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    cu = conn.execute(
        "SELECT AVG(leg1_cu_consumed + leg2_cu_consumed), AVG(leg1_signatures + leg2_signatures) "
        "FROM round_trip_cohort WHERE leg1_cu_consumed IS NOT NULL"
    ).fetchone()
    if not row or row[1] is None:
        print("  sem dados de priority fee filtrada")
        return {}
    p25, p50, p75, p90, p99, gmed, gp90 = row
    cu_total, sigs_total = (cu or (None, None))

    if p50 and p50 > 0 and p99:
        spread = p99 / p50
        print()
        print(f"  filtrada por pool: p25 {p25} | p50 {p50} | p75 {p75} | p90 {p90} | p99 {p99}")
        if gmed and gmed > 0 and gp90:
            print(f"  global (referencia): mediana {gmed} | p90 {gp90} "
                  f"(spread {gp90/gmed:.0f}x)")
        print(f"  SPREAD p99/p50 DEPOIS DO FILTRO: {spread:.1f}x")

    if cu_total is None or sigs_total is None or sol_price is None:
        print()
        print("  Sem cu_consumed/assinaturas para converter em break-even por percentil.")
        return {"p50": p50, "p99": p99}

    print()
    print(f"  Break-even em US${THESIS_POSITION_USDC:g}, so componente de rede,")
    print(f"  com cu={cu_total:.0f} e {sigs_total:.0f} assinaturas nas duas pernas:")
    print()
    print(f"    {'PERCENTIL':>10} | {'cu_price':>10} | {'REDE US$':>10} | {'% DA POSICAO':>13}")
    print("    " + "-" * 54)
    out = {}
    for name, val in [("p25", p25), ("p50", p50), ("p75", p75), ("p90", p90), ("p99", p99)]:
        if val is None:
            continue
        lamports = 5000 * sigs_total + (val * cu_total) / 1_000_000
        usd = lamports / LAMPORTS_PER_SOL * sol_price
        pct = usd / THESIS_POSITION_USDC * 100
        print(f"    {name:>10} | {val:>10.0f} | {usd:>10.6f} | {pct:>12.4f}%")
        out[name] = {"cu_price": val, "network_usd": usd, "network_pct": pct}

    if "p50" in out and "p99" in out and out["p50"]["network_pct"] > 0:
        var = out["p99"]["network_pct"] / out["p50"]["network_pct"]
        print()
        print(f"  Custo de rede em US$1 varia {var:.1f}x entre p50 e p99.")
        if var >= 3:
            print("  [!] Variacao dessa ordem inviabiliza alvo FIXO de lucro por trade.")
            print("      O alvo tem que ser condicional a priority fee no momento da")
            print("      decisao, ou o agente precisa recusar trade quando a fee estiver")
            print("      na cauda. Isso e parametro de desenho, nao detalhe.")
    return out


def drift_stats(conn, size=None):
    """
    |drift| por horizonte de latência.

    A mediana COM SINAL tende a zero (o preço anda para os dois lados).
    O custo está na MAGNITUDE: metade das vezes o movimento é adverso, e é
    esse caso que define o break-even conservador.
    """
    q = (
        "SELECT nominal_horizon_ms, drift_bps FROM quote_drift_cohort "
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
            "SELECT drift_bps FROM quote_drift_cohort "
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
    paired = conn.execute(
        "SELECT COUNT(*) FROM quote_drift_cohort "
        "WHERE drift_bps IS NOT NULL AND mid_drift_bps IS NOT NULL"
    ).fetchone()[0]
    mismatched = conn.execute(
        "SELECT COUNT(*) FROM quote_drift_cohort "
        "WHERE drift_bps IS NOT NULL AND mid_drift_bps IS NOT NULL "
        "AND route_match = 0"
    ).fetchone()[0]
    unknown_route = conn.execute(
        "SELECT COUNT(*) FROM quote_drift_cohort "
        "WHERE drift_bps IS NOT NULL AND mid_drift_bps IS NOT NULL "
        "AND route_match IS NULL"
    ).fetchone()[0]
    route_changed = conn.execute(
        "SELECT COUNT(*) FROM quote_drift_cohort WHERE size_route_changed = 1"
    ).fetchone()[0]

    # Jupiter roteia por tamanho: sonda pode sair single-hop e a real abrir
    # split multi-venue. Nesse caso sizeComponentBps mistura ROTA com TAMANHO
    # e a observacao nao entra na calibracao.
    rows = list(
        conn.execute(
            """SELECT nominal_horizon_ms, position_size_usdc,
                      drift_bps, mid_drift_bps, size_component_bps
               FROM quote_drift_cohort
               WHERE drift_bps IS NOT NULL AND mid_drift_bps IS NOT NULL
                 AND route_match = 1
               ORDER BY nominal_horizon_ms, position_size_usdc"""
        )
    )
    print()
    print("-" * 78)
    print("  CALIBRACAO: quote_drift vs sonda de notional despresivel")
    print("  (sonda cotada em par, a poucos ms — isola movimento de preco de tamanho)")
    print("-" * 78)
    print()
    print(f"  pares sonda/tamanho: {paired}")
    print(f"  EXCLUIDOS por rota divergente (route_match=0): {mismatched}")
    if unknown_route:
        print(f"  sem info de rota (versao anterior do script): {unknown_route}")
    if route_changed:
        print(f"  alerta: {route_changed} quotes mudaram de rota entre t0 e t0+horizonte")
        print(f"          — nesses pontos o 'drift' carrega descontinuidade de roteamento")
    if mismatched and paired:
        print(f"  -> {mismatched/paired*100:.0f}% das observacoes tinham rota diferente entre")
        print(f"     sonda e tamanho real. sizeComponentBps so e atribuivel ao TAMANHO")
        print(f"     nas que sobraram.")

    if not rows:
        print()
        print("  Nenhum par com rota coincidente — calibracao nao calculada.")
        print("  Se todos divergem, a sonda de US$0,10 nao representa a liquidez que")
        print("  os tamanhos reais tocam, e precisa de outro notional.")
        return {}

    out = {"pairs": paired, "excluded_route_mismatch": mismatched,
           "unknown_route": unknown_route, "size_route_changed": route_changed}
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
        "SELECT nominal_horizon_ms, drift_bps FROM quote_drift_cohort "
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
            "SELECT drift_bps FROM quote_drift_cohort "
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
            denom = n_eff if (n_eff and n_eff > 0) else n
            p, upper = 1.0 / denom, True  # teto, nao zero
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
        if n_eff > 0:
            print(f"  Resolucao de cauda pelo n efetivo: {100/n_eff:.2f}%")
        print("  PRELIMINAR: revert_rate_floor definitivo sai no Day 2-3 sobre historico.")

        if n == 0:
            print()
            print("  Sem amostras de drift neste horizonte — curva nao desenhada.")
            continue

        if n < MIN_DRIFT_SAMPLES:
            print()
            print(f"  n abaixo de {MIN_DRIFT_SAMPLES} — curva nao desenhada.")
            continue

        # ── Piso economico do MEV ──
        if attacker_cost:
            print()
            print("  LIMIAR DE TRIAGEM DO SEARCHER — ESTIMATIVA MOLE, NAO MEDIDA")
            print("  " + "-" * 68)
            print("    MEV na Solana opera por bundle com tip leiloado: o tip acompanha")
            print("    a extracao, entao custo marginal de tx NAO forma piso. O que protege")
            print("    a posicao pequena e o overhead fixo de triagem do searcher.")
            print(f"    proxy grosseiro do overhead (2 tx, priority p90): ${attacker_cost:.6f}")
            max_safe_tol = attacker_cost * 10_000 / pos
            print(f"    TOLERANCIA em que US${pos:g} atinge o limiar estimado: "
                  f"~{max_safe_tol:.0f} bps")
            print("    ORDEM DE GRANDEZA, nao garantia. Operar com FOLGA GRANDE abaixo,")
            print("    nunca colado. Confirmacao so com dado real.")
            print()
            print(f"    {'TOL bps':>8} | {'EXTRACAO US$':>13} | {'LIMIAR POSICAO':>15} | {'US$' + format(pos, 'g'):>10}")
            print("    " + "-" * 60)
            for f in floors:
                tol = f["tol_bps"]
                extraction = pos * (tol / 10_000)
                threshold = attacker_cost * 10_000 / tol
                below = pos < threshold
                print(f"    {tol:>8} | {extraction:>13.6f} | {threshold:>15.2f} | "
                      f"{'sob limiar' if below else 'exposto':>10}")

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
            mev = "sob limiar" if r["position_below_mev_floor"] else "exposto"
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
            print(f"  US${pos:g} fica sob o limiar ESTIMADO de triagem em toda a grade.")
            print("  Estimativa, nao medicao: o tip leiloado acompanha a extracao, entao")
            print("  o que segura o ataque e a triagem do searcher, nao aritmetica. Sob ela")
            print("  a curva e MONOTONICAMENTE DECRESCENTE e nao existe minimo interno.")
            print()
            if zeroing:
                print(f"  TOLERANCIA MINIMA QUE ZERA REVERSAO (ate a resolucao): "
                      f"{zeroing['tol_bps']} bps -> {zeroing['break_even_pct']:.3f}%")
            else:
                print("  Nenhuma tolerancia da grade zerou reversao na amostra.")
            print()
            print("  Vantagem PROVAVEL da microposicao — a confirmar na Fase 2 com")
            print("  execucao real. Nao tratar como protecao garantida.")
        elif rows:
            best = min(rows, key=lambda r: r["break_even_with_sandwich_pct"])
            print()
            print(f"  MINIMO: {best['tol_bps']} bps -> {best['break_even_with_sandwich_pct']:.3f}%")

        out[f"{horizon}ms"] = {
            "rows": rows,
            "n_raw": n,
            "n_effective": n_eff,
            "rho_lag1": rho,
            "tolerance_at_triage_threshold_bps_UNMEASURED":
                (attacker_cost * 10_000 / pos) if attacker_cost else None,
            "searcher_overhead_proxy_usdc": attacker_cost,
            "mev_estimate_measured": False,
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
    # Primeiro de tudo: as views de coorte, que todas as queries abaixo usam.
    cohort = build_cohort(conn)

    runs = conn.execute(
        "SELECT COUNT(*), MIN(timestamp_utc), MAX(timestamp_utc) FROM run"
    ).fetchone()
    n_runs, first, last = runs

    ok = conn.execute("SELECT COUNT(*) FROM round_trip_cohort WHERE error IS NULL").fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM round_trip_cohort WHERE error IS NOT NULL").fetchone()[0]

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
            "SELECT DISTINCT error FROM round_trip_cohort WHERE error IS NOT NULL LIMIT 10"
        ):
            print(f"    - {err}")
        print()
        print("  Sem dado real não há tabela. Corrija o acesso de rede e rode de novo.")
        print("=" * 78)
        return

    integrity = integrity_section(conn)

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
            "SELECT DISTINCT position_size_usdc FROM round_trip_cohort "
            "WHERE error IS NULL ORDER BY position_size_usdc"
        )
    ]

    for size in sizes:
        rows = conn.execute(
            """SELECT swap_loss_usdc, swap_loss_pct, net_lamports_median, net_lamports_p90
               FROM round_trip_cohort WHERE position_size_usdc = ? AND error IS NULL""",
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

    decomp = decomposition_section(conn, sol_price)
    dispersion = dispersion_section(conn, sol_price)

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
           FROM round_trip_cohort WHERE error IS NULL
           GROUP BY position_size_usdc, leg1_venues, leg2_venues
           ORDER BY position_size_usdc"""
    ):
        print(f"  ${format(size, 'g'):<7} USDC->SOL [{h1}] {v1 or '?'}  |  SOL->USDC [{h2}] {v2 or '?'}  (n={n})")

    # ── Garantia da emenda 3 ──
    leaked = conn.execute(
        "SELECT COUNT(*) FROM round_trip_cohort "
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
        "SELECT COUNT(*) FROM round_trip_cohort WHERE tx_failure_rate IS NOT NULL"
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
                "cohort": cohort,
                "integrity": integrity,
                "decomposition": decomp,
                "priority_fee_dispersion": dispersion,
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
