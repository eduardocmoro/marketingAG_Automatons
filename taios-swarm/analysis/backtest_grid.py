#!/usr/bin/env python3
"""
TAIOS-Swarm — Day 3: grid de agentes contra população de controle.

NÃO é o motor de evolução. É o teste que decide se vale construir um: se
nenhum agente de um grid supera o controle, evolução não resolve — ela busca
o mesmo espaço com mais afinco.

DISCIPLINA (emenda 1, do README):
  a) Split temporal — grid roda na PRIMEIRA metade; validação na segunda,
     passagem única, sem re-seleção.
  b) Controle — mesma grade sobre retornos EMBARALHADOS. O melhor real tem
     que superar o melhor do controle, não superar zero.
  c) Reporte — nº testados, nº sobreviventes, net_EV com IC, e o net_EV do
     melhor do controle.

REALISMO (o que impede o resultado de ser ficção):
  - LONG-ONLY. Swap spot em Solana compra SOL com USDC e vende de volta.
    Não há venda a descoberto sem protocolo de empréstimo ou perp. Permitir
    short validaria estratégia que não se pode executar.
  - Entrada no fechamento do candle SEGUINTE ao sinal. Você não negocia o
    fechamento que acabou de observar.
  - Alvo e stop no mesmo candle: assume STOP primeiro. Conservador.
  - Normalização do indicador calculada SÓ na metade de treino e aplicada às
    duas. Usar o desvio da amostra inteira é lookahead.
  - Custo lido do day1_summary.json, não constante chutada.

O QUE ESTE BACKTEST NÃO COBRA:
  O custo de spread não foi medido (round trip cotado saiu negativo — ver
  README). Só o custo de REDE entra. Portanto todo resultado aqui é um
  TETO OTIMISTA: a performance real é pior pelo spread não medido.

Uso:
    python3 analysis/backtest_grid.py
    python3 analysis/backtest_grid.py --position 5 --min-trades 30
"""

import argparse
import json
import math
import random
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "market.sqlite"
DAY1_SUMMARY = ROOT / "measurements" / "day1_summary.json"

# Grade de parâmetros. Pequena de propósito: o objetivo é detectar sinal,
# não achar o ótimo. Ótimo sem sinal é sobreajuste.
KINDS = ["momentum", "reversion"]
WINDOWS = [15, 60, 240]          # candles de 1 min
ENTRY_Z = [0.5, 1.0, 2.0]        # em desvios-padrão do indicador (treino)
TARGETS_BPS = [20, 50, 150]
STOPS_BPS = [20, 50, 150]
MAX_HOLD = [60, 480]             # minutos

SEED = 20260918


# ── Custo ────────────────────────────────────────────────────────────

def round_trip_cost_bps(position_usdc: float, summary_path: Path = None) -> tuple:
    """
    Custo de round trip em bps, do Day 1. SÓ o componente de REDE.

    O custo de spread não foi medido (round trip cotado deu negativo), então
    fica FORA. Isso torna o resultado um teto otimista, e o relatório diz.
    """
    path = summary_path or DAY1_SUMMARY
    if not path.exists():
        print(f"ERRO: {path} não existe. Rode analysis/day1_report.py antes.")
        sys.exit(1)
    d = json.loads(path.read_text())
    rows = d.get("summary", [])
    exact = [r for r in rows if abs(r["position_usdc"] - position_usdc) < 1e-9]
    if not exact:
        sizes = sorted(r["position_usdc"] for r in rows)
        print(f"ERRO: US${position_usdc:g} não foi medido. Medidos: {sizes}")
        sys.exit(1)
    r = exact[0]
    net_med = r.get("network_median_usdc")
    net_p90 = r.get("network_p90_usdc")
    if net_med is None:
        print("ERRO: custo de rede ausente no day1_summary.json.")
        sys.exit(1)
    return (net_med / position_usdc * 10_000,
            (net_p90 / position_usdc * 10_000) if net_p90 else None)


# ── Série ────────────────────────────────────────────────────────────

def load_closes(db: Path, symbol: str, interval: str) -> list:
    conn = sqlite3.connect(db)
    rows = [r[0] for r in conn.execute(
        "SELECT close FROM candle WHERE symbol=? AND interval=? ORDER BY open_time_ms",
        (symbol, interval))]
    highs = [r[0] for r in conn.execute(
        "SELECT high FROM candle WHERE symbol=? AND interval=? ORDER BY open_time_ms",
        (symbol, interval))]
    lows = [r[0] for r in conn.execute(
        "SELECT low FROM candle WHERE symbol=? AND interval=? ORDER BY open_time_ms",
        (symbol, interval))]
    conn.close()
    if not rows:
        print(f"ERRO: sem candles de {symbol} {interval}. Rode ingest_candles.py.")
        sys.exit(1)
    return rows, highs, lows


def shuffled_series(closes: list, rng: random.Random) -> tuple:
    """
    Controle: embaralha os retornos e reconstrói o preço.

    Preserva a DISTRIBUIÇÃO de retornos e destrói a ESTRUTURA TEMPORAL. Um
    agente que lucra aqui está lucrando por sorte da seleção, não por sinal.
    High e low são reconstruídos proporcionalmente para o stop/alvo continuar
    tendo o mesmo alcance intra-candle.
    """
    rets = [closes[i] / closes[i - 1] for i in range(1, len(closes))]
    rng.shuffle(rets)
    out = [closes[0]]
    for r in rets:
        out.append(out[-1] * r)
    # Amplitude intra-candle tipica, aplicada simetricamente.
    hi = [c * 1.0004 for c in out]
    lo = [c * 0.9996 for c in out]
    return out, hi, lo


# ── Indicadores ──────────────────────────────────────────────────────

def indicator(closes: list, kind: str, window: int) -> list:
    """
    Sinal em cada candle, usando SOMENTE dados até aquele candle.

    momentum  : retorno acumulado da janela  (entra comprado quando SOBE)
    reversion : desvio do preço frente à média da janela (entra na QUEDA)
    """
    n = len(closes)
    out = [None] * n
    if kind == "momentum":
        for i in range(window, n):
            prev = closes[i - window]
            if prev > 0:
                out[i] = closes[i] / prev - 1.0
    else:  # reversion
        acc = sum(closes[:window])
        for i in range(window, n):
            acc += closes[i] - closes[i - window]
            mean = acc / window
            if mean > 0:
                out[i] = closes[i] / mean - 1.0
    return out


def train_sigma(values: list, split: int) -> float:
    """Desvio do indicador na metade de TREINO. Usar a amostra toda é lookahead."""
    sample = [v for v in values[:split] if v is not None]
    if len(sample) < 100:
        return 0.0
    return statistics.pstdev(sample)


# ── Execução de um agente ────────────────────────────────────────────

def run_agent(closes, highs, lows, ind, sigma, kind, entry_z,
              target_bps, stop_bps, max_hold, cost_bps, lo, hi):
    """
    LONG-ONLY. Devolve a lista de retornos líquidos por trade, em bps.

    Entrada no fechamento do candle SEGUINTE ao sinal.
    Saída por alvo, stop ou tempo. Alvo e stop no mesmo candle -> stop.
    """
    if sigma <= 0:
        return []
    thr = entry_z * sigma
    trades = []
    i = lo
    n = min(hi, len(closes) - 2)

    while i < n:
        s = ind[i]
        if s is None:
            i += 1
            continue
        # momentum entra quando sobe; reversao entra quando cai
        fires = (s > thr) if kind == "momentum" else (s < -thr)
        if not fires:
            i += 1
            continue

        entry_idx = i + 1                      # nao se negocia o close observado
        entry = closes[entry_idx]
        if entry <= 0:
            i += 1
            continue
        tgt = entry * (1 + target_bps / 10_000)
        stp = entry * (1 - stop_bps / 10_000)

        exit_idx = min(entry_idx + max_hold, n)
        gross = None
        for j in range(entry_idx + 1, exit_idx + 1):
            if lows[j] <= stp:                 # stop tem prioridade (conservador)
                gross = -stop_bps
                break
            if highs[j] >= tgt:
                gross = target_bps
                break
        if gross is None:                      # saida por tempo
            j = exit_idx
            gross = (closes[j] / entry - 1) * 10_000

        trades.append(gross - cost_bps)
        i = j + 1                              # sem posicoes sobrepostas

    return trades


def summarize(trades: list) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "mean_bps": None, "ci_lo": None, "ci_hi": None,
                "total_bps": 0.0, "win_rate": None}
    mean = statistics.fmean(trades)
    total = sum(trades)
    wins = sum(1 for t in trades if t > 0) / n
    if n > 2:
        se = statistics.stdev(trades) / math.sqrt(n)
        ci = (mean - 1.96 * se, mean + 1.96 * se)
    else:
        ci = (None, None)
    return {"trades": n, "mean_bps": mean, "ci_lo": ci[0], "ci_hi": ci[1],
            "total_bps": total, "win_rate": wins}


# ── Grid ─────────────────────────────────────────────────────────────

def agent_grid():
    for kind in KINDS:
        for w in WINDOWS:
            for z in ENTRY_Z:
                for t in TARGETS_BPS:
                    for s in STOPS_BPS:
                        for h in MAX_HOLD:
                            yield {"kind": kind, "window": w, "entry_z": z,
                                   "target_bps": t, "stop_bps": s, "max_hold": h}


def evaluate_all(closes, highs, lows, split, cost_bps, label, min_trades):
    """Roda a grade inteira. Indicador e calculado uma vez por (kind, window)."""
    results = []
    cache = {}
    agents = list(agent_grid())
    for idx, a in enumerate(agents, 1):
        key = (a["kind"], a["window"])
        if key not in cache:
            ind = indicator(closes, a["kind"], a["window"])
            cache[key] = (ind, train_sigma(ind, split))
        ind, sigma = cache[key]

        tr_train = run_agent(closes, highs, lows, ind, sigma, a["kind"],
                             a["entry_z"], a["target_bps"], a["stop_bps"],
                             a["max_hold"], cost_bps, a["window"], split)
        tr_val = run_agent(closes, highs, lows, ind, sigma, a["kind"],
                           a["entry_z"], a["target_bps"], a["stop_bps"],
                           a["max_hold"], cost_bps, split, len(closes))
        results.append({**a, "train": summarize(tr_train), "val": summarize(tr_val)})
        if idx % 50 == 0:
            print(f"    {label}: {idx}/{len(agents)} agentes", end="\r", flush=True)
    print(" " * 60, end="\r")
    return results


def best_by_train(results, min_trades):
    """Seleção SÓ pelo treino. Olhar a validação para escolher a invalida."""
    eligible = [r for r in results
                if r["train"]["trades"] >= min_trades
                and r["train"]["mean_bps"] is not None]
    if not eligible:
        return None
    return max(eligible, key=lambda r: r["train"]["mean_bps"])


def fmt(v, nd=2):
    return "—" if v is None else f"{v:.{nd}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--symbol", default="SOLUSDC")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--position", type=float, default=5.0)
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--day1", type=Path, default=None,
                    help="caminho do day1_summary.json (padrão: measurements/)")
    args = ap.parse_args()

    cost_med, cost_p90 = round_trip_cost_bps(args.position, args.day1)
    closes, highs, lows = load_closes(args.db, args.symbol, args.interval)
    split = len(closes) // 2

    print("=" * 78)
    print("  TAIOS-SWARM — GRID DE AGENTES vs CONTROLE")
    print("=" * 78)
    print(f"  serie        : {args.symbol} {args.interval}, {len(closes):,} candles")
    print(f"  split        : treino 0..{split:,} | validacao {split:,}..{len(closes):,}")
    print(f"  posicao      : US${args.position:g}")
    print(f"  custo de rede: {cost_med:.2f} bps por round trip (mediana)"
          + (f" | {cost_p90:.2f} bps (p90)" if cost_p90 else ""))
    print(f"  agentes      : {len(list(agent_grid()))}")
    print(f"  min trades   : {args.min_trades} para elegibilidade")
    print()
    print("  ATENCAO: custo de SPREAD nao entra (nao foi medido — ver README).")
    print("  Todo numero abaixo e TETO OTIMISTA; o real e pior.")
    print()

    print("  [1/2] Serie real...")
    real = evaluate_all(closes, highs, lows, split, cost_med, "real", args.min_trades)

    print("  [2/2] Controle (retornos embaralhados)...")
    rng = random.Random(SEED)
    c_closes, c_highs, c_lows = shuffled_series(closes, rng)
    ctrl = evaluate_all(c_closes, c_highs, c_lows, split, cost_med, "controle",
                        args.min_trades)

    best_real = best_by_train(real, args.min_trades)
    best_ctrl = best_by_train(ctrl, args.min_trades)

    survivors = [r for r in real
                 if r["train"]["trades"] >= args.min_trades
                 and r["val"]["trades"] >= args.min_trades
                 and r["val"]["mean_bps"] is not None
                 and r["val"]["ci_lo"] is not None
                 and r["val"]["ci_lo"] > 0]

    print()
    print("=" * 78)
    print("  RESULTADO")
    print("=" * 78)
    print()
    print(f"  agentes testados            : {len(real)}")
    print(f"  sobreviventes (IC>0 na val) : {len(survivors)}")

    if best_real:
        v, t = best_real["val"], best_real["train"]
        print()
        print("  MELHOR DO TREINO (selecionado sem olhar a validacao)")
        print(f"    {best_real['kind']} w={best_real['window']} z={best_real['entry_z']} "
              f"alvo={best_real['target_bps']} stop={best_real['stop_bps']} "
              f"hold={best_real['max_hold']}")
        print(f"    treino    : {t['trades']:>5} trades | "
              f"{fmt(t['mean_bps'])} bps/trade | acerto {fmt((t['win_rate'] or 0)*100,1)}%")
        print(f"    VALIDACAO : {v['trades']:>5} trades | "
              f"{fmt(v['mean_bps'])} bps/trade | IC95 [{fmt(v['ci_lo'])}, {fmt(v['ci_hi'])}]")

    if best_ctrl:
        cv = best_ctrl["val"]
        print()
        print("  MELHOR DO CONTROLE (retornos embaralhados)")
        print(f"    treino    : {fmt(best_ctrl['train']['mean_bps'])} bps/trade")
        print(f"    VALIDACAO : {cv['trades']:>5} trades | "
              f"{fmt(cv['mean_bps'])} bps/trade | IC95 [{fmt(cv['ci_lo'])}, {fmt(cv['ci_hi'])}]")

    print()
    print("  " + "=" * 74)
    if not best_real or not best_ctrl:
        print("  VEREDITO: sem agentes elegiveis. Baixe --min-trades ou amplie a grade.")
    else:
        rv = best_real["val"]["mean_bps"]
        cv = best_ctrl["val"]["mean_bps"]
        print(f"  real {fmt(rv)} bps/trade  vs  controle {fmt(cv)} bps/trade")
        if rv is None or cv is None:
            print("  VEREDITO: dados insuficientes.")
        elif rv <= cv:
            print("  VEREDITO: o melhor agente real NAO supera o melhor do controle.")
            print("  Nao ha sinal detectavel nesta grade. Evolucao busca o mesmo")
            print("  espaco com mais afinco e nao cria sinal que nao existe.")
        elif best_real["val"]["ci_lo"] is not None and best_real["val"]["ci_lo"] <= 0:
            print("  VEREDITO: supera o controle, mas o IC95 da validacao inclui zero.")
            print("  Indicio, nao resultado. Precisa de mais amostra antes de decidir.")
        else:
            print("  VEREDITO: supera o controle E o IC95 da validacao exclui zero.")
            print("  Primeiro indicio real de edge. Falta: classificar por regime")
            print("  (>= 2 regimes distintos) e somar o custo de spread nao medido.")
    print("  " + "=" * 74)

    out = ROOT / "data" / "grid_result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "position_usdc": args.position,
        "cost_bps_median": cost_med,
        "cost_bps_p90": cost_p90,
        "spread_cost_included": False,
        "candles": len(closes), "split": split,
        "agents_tested": len(real), "survivors": len(survivors),
        "best_real": best_real, "best_control": best_ctrl,
    }, indent=2), "utf-8")
    print(f"\n  Detalhe em JSON: {out}")


if __name__ == "__main__":
    main()
