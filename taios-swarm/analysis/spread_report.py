#!/usr/bin/env python3
"""
TAIOS-Swarm — Day 5: relatório do spread CEX vs DEX.

A pergunta: a diferença entre os dois venues supera o custo de capturá-la?

Custo de uma arbitragem = taxa taker da Binance + taxa de rede da Solana.
A taxa da Binance é ~10 bps e não escala com o tamanho; a de rede é fixa em
dólar e encolhe como percentual quando a posição cresce. Em US$50 a Binance
responde por mais de 90% do custo.

TRÊS DESFECHOS, e o relatório diz qual é:
  - diferença consistentemente acima do custo -> oportunidade, vale construir
  - diferença abaixo do custo                 -> arbitrado, resposta dada
  - diferença que aparece e some               -> corrida de latência

Uso:
    python3 analysis/spread_report.py
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JSONL = ROOT / "data" / "spread_cex_dex.jsonl"
DAY1_SUMMARY = ROOT / "measurements" / "day1_summary.json"

MAX_VENUE_GAP_MS = 400


def network_bps(position_usdc, summary_path=None):
    """Taxa de rede da Solana em bps, do Day 1. None se não medido."""
    path = summary_path or DAY1_SUMMARY
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text())
        rows = d.get("summary", [])
        exact = [r for r in rows if abs(r["position_usdc"] - position_usdc) < 1e-9]
        if exact and exact[0].get("network_median_usdc"):
            return exact[0]["network_median_usdc"] / position_usdc * 10_000
        # Sem o tamanho exato: a taxa e fixa em dolar, entao reescala.
        with_net = [r for r in rows if r.get("network_median_usdc")]
        if with_net:
            ref = with_net[0]
            return ref["network_median_usdc"] / position_usdc * 10_000
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return None


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    i = min(int(len(sorted_vals) * p), len(sorted_vals) - 1)
    return sorted_vals[i]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    ap.add_argument("--day1", type=Path, default=None)
    args = ap.parse_args()

    if not args.jsonl.exists():
        print(f"ERRO: {args.jsonl} não existe. Rode scripts/measure-spread.mjs.")
        sys.exit(1)

    runs = []
    malformed = 0
    for line in args.jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            malformed += 1

    if not runs:
        print("ERRO: nenhuma amostra válida.")
        sys.exit(1)

    taker = runs[-1].get("binanceTakerBps", 10)

    # Agrupa por tamanho, separando contaminadas
    by_size, contaminated, errors = {}, 0, 0
    for r in runs:
        for s in r.get("samples", []):
            if s.get("error"):
                errors += 1
                continue
            if s.get("contaminated"):
                contaminated += 1
                continue
            by_size.setdefault(s["sizeUsdc"], []).append(s)

    print("=" * 78)
    print("  TAIOS-SWARM — SPREAD EXECUTAVEL CEX vs DEX")
    print("=" * 78)
    print(f"  execucoes    : {len(runs)}")
    print(f"  amostras     : {sum(len(v) for v in by_size.values())} validas, "
          f"{contaminated} contaminadas (gap > {MAX_VENUE_GAP_MS}ms), {errors} com erro")
    print(f"  taxa Binance : {taker} bps (taker spot)")
    print(f"  periodo      : {runs[0]['timestampUtc'][:16]} -> {runs[-1]['timestampUtc'][:16]}")
    if malformed:
        print(f"  ({malformed} linha(s) malformada(s) ignorada(s))")

    if contaminated:
        print()
        print(f"  {contaminated} amostras excluidas por intervalo entre venues acima de")
        print(f"  {MAX_VENUE_GAP_MS}ms. Nelas a deriva de preco compete com o spread")
        print("  medido, entao elas medem outra coisa.")

    if not by_size:
        print("\n  Nenhuma amostra valida.")
        return

    print()
    print("=" * 78)
    print("  DIFERENCA BRUTA POR TAMANHO (antes de qualquer taxa)")
    print("=" * 78)
    print()
    print(f"  {'TAM':>6} | {'n':>4} | {'DEX->CEX bps':>26} | {'CEX->DEX bps':>26}")
    print(f"  {'':>6} | {'':>4} | {'p50':>7} {'p90':>7} {'max':>9} | "
          f"{'p50':>7} {'p90':>7} {'max':>9}")
    print("  " + "-" * 72)

    summary = {}
    for size in sorted(by_size):
        rows = by_size[size]
        d2c = sorted(r["dexToCexBps"] for r in rows)
        c2d = sorted(r["cexToDexBps"] for r in rows)
        summary[size] = {"n": len(rows), "d2c": d2c, "c2d": c2d}
        print(f"  {'$' + format(size, 'g'):>6} | {len(rows):>4} | "
              f"{pct(d2c,0.5):>7.1f} {pct(d2c,0.9):>7.1f} {d2c[-1]:>9.1f} | "
              f"{pct(c2d,0.5):>7.1f} {pct(c2d,0.9):>7.1f} {c2d[-1]:>9.1f}")

    # ── Liquido ──
    print()
    print("=" * 78)
    print("  LIQUIDO: DIFERENCA MENOS CUSTO DE CAPTURA")
    print("=" * 78)
    print()
    print(f"  {'TAM':>6} | {'CUSTO':>22} | {'MELHOR DIRECAO LIQUIDA':>30}")
    print(f"  {'':>6} | {'CEX':>6} {'rede':>6} {'total':>7} | "
          f"{'p50':>8} {'p90':>8} {'max':>10}")
    print("  " + "-" * 72)

    any_positive = False
    for size in sorted(by_size):
        net_bps = network_bps(size, args.day1)
        if net_bps is None:
            net_bps = 0.0
            net_label = "n/d"
        else:
            net_label = f"{net_bps:.2f}"
        total_cost = taker + net_bps

        s = summary[size]
        # Em cada amostra, a melhor das duas direcoes
        best = sorted(max(a, b) for a, b in zip(
            sorted(s["d2c"]), sorted(s["c2d"])))
        p50 = pct(best, 0.5) - total_cost
        p90 = pct(best, 0.9) - total_cost
        mx = best[-1] - total_cost
        if p50 > 0 or p90 > 0:
            any_positive = True
        flag = "" if p90 <= 0 else "  <-- positivo no p90"
        print(f"  {'$' + format(size, 'g'):>6} | {taker:>6.1f} {net_label:>6} "
              f"{total_cost:>7.2f} | {p50:>8.2f} {p90:>8.2f} {mx:>10.2f}{flag}")
        summary[size]["net_p50"] = p50
        summary[size]["net_p90"] = p90
        summary[size]["cost_bps"] = total_cost

    # ── Persistencia: oportunidade que some em segundos e corrida de latencia ──
    print()
    print("=" * 78)
    print("  VEREDITO")
    print("=" * 78)
    print()
    if not any_positive:
        print("  A diferenca entre os venues NAO cobre o custo de captura em nenhum")
        print("  tamanho, nem no p90. SOL/USDC esta arbitrado — bots profissionais")
        print("  comprimem essa diferenca abaixo do que custa captura-la.")
        print()
        print("  Isto responde a opcao 4 para ESTE par. A mesma infraestrutura")
        print("  serve para par ilíquido ou listagem nova, onde menos gente arbitra:")
        print("  troque BINANCE_SYMBOL e os mints e rode de novo.")
    else:
        print("  Ha tamanho em que a diferenca supera o custo. ANTES de construir:")
        print()
        print("  1. PERSISTENCIA. Se a oportunidade aparece e some em segundos, e")
        print("     corrida de latencia contra quem tem servidor colocado, e voce")
        print("     perde. Rode com --interval 2 e veja se sobrevive entre amostras.")
        print("  2. CAPITAL DOS DOIS LADOS. Arbitragem real exige inventario parado")
        print("     na Binance E na carteira Solana. Isso e custo de capital que")
        print("     esta medicao nao cobra.")
        print("  3. RISCO DE PERNA SOLTA. Se um lado executa e o outro nao, voce")
        print("     fica com posicao direcional nao intencional.")

    # Spread interno de cada venue, para entender a origem
    print()
    print("  SPREAD INTERNO DE CADA VENUE (compra menos venda, mesmo instante)")
    print("  " + "-" * 72)
    for size in sorted(by_size):
        rows = by_size[size]
        ji = sorted(r["jupInternalBps"] for r in rows if r.get("jupInternalBps") is not None)
        bi = sorted(r["binInternalBps"] for r in rows if r.get("binInternalBps") is not None)
        if ji and bi:
            print(f"    ${format(size,'g'):>5}: Jupiter {pct(ji,0.5):>7.1f} bps | "
                  f"Binance {pct(bi,0.5):>6.1f} bps")
    print()
    print("    Este e o custo de ida e volta DENTRO de cada venue. Se o spread")
    print("    interno da Jupiter for negativo, e o cruzamento de market makers")
    print("    que o Day 1 detectou — cotacao, nao necessariamente execucao.")

    out = ROOT / "data" / "spread_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "runs": len(runs), "taker_bps": taker,
        "contaminated": contaminated, "errors": errors,
        "by_size": {str(k): {"n": v["n"], "net_p50": v.get("net_p50"),
                             "net_p90": v.get("net_p90"), "cost_bps": v.get("cost_bps")}
                    for k, v in summary.items()},
    }, indent=2), "utf-8")
    print(f"\n  Detalhe: {out}")


if __name__ == "__main__":
    main()
