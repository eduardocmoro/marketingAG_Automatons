#!/usr/bin/env python3
"""
TAIOS-Swarm — Day 4: varredura de poder preditivo de sinais.

MUDANÇA DE MÉTODO em relação ao backtest_grid.

O grid testava 324 estratégias e cada uma fazia ~70 operações: 129 mil
candles viravam 70 pontos de evidência. Aqui mede-se o sinal DIRETO contra o
retorno futuro, usando todos os pontos. Ordens de magnitude mais poder.

Só o que sobreviver aqui vira estratégia. Montar estratégia em cima de sinal
que não prevê nada é construir máquina para procurar o que não existe.

MÉTRICA PRINCIPAL: spread de decil.
Ordena os candles pelo valor do sinal, separa em 10 faixas, e mede o retorno
futuro médio da faixa mais alta menos o da mais baixa. É interpretável
direto: "comprando o decil de cima e vendendo o de baixo, capturo N bps
brutos". Serve para long e para short — se o sinal prevê queda, o spread sai
negativo, e isso é informação, não fracasso.

DISCIPLINA:
  - Amostras NÃO SOBREPOSTAS. Com horizonte de H minutos, observações
    consecutivas compartilham H-1 minutos de retorno futuro. Isso infla o
    t enormemente. Usa-se passo H, reduzindo n para n/H e eliminando a
    sobreposição — conservador e sem correção estatística discutível.
  - Split temporal: mede no treino, CONFIRMA na validação.
  - Multiplicidade: com dezenas de pares (sinal, horizonte), o relatório
    imprime o limiar de Bonferroni e quantos achados o acaso produziria.
  - Bruto separado do líquido: o custo é fixo em dólar, então um sinal bom
    pode ficar invisível em posição pequena. Reporta-se o bruto e o líquido
    em vários tamanhos.

Uso:
    python3 analysis/signal_scan.py
    python3 analysis/signal_scan.py --symbol SOLUSDC --horizons 5,15,60
"""

import argparse
import json
import math
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "market.sqlite"
DAY1_SUMMARY = ROOT / "measurements" / "day1_summary.json"

WINDOWS = [5, 15, 60, 240]
HORIZONS = [5, 15, 60, 240]
DECILES = 10


# ── Carga ────────────────────────────────────────────────────────────

def load(db: Path, symbol: str, interval: str):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT open_time_ms, close, high, low, volume, trades, taker_buy_base "
        "FROM candle WHERE symbol=? AND interval=? ORDER BY open_time_ms",
        (symbol, interval)).fetchall()
    conn.close()
    if not rows:
        print(f"ERRO: sem candles de {symbol} {interval}.")
        sys.exit(1)
    return {
        "t": [r[0] for r in rows],
        "close": [r[1] for r in rows],
        "high": [r[2] for r in rows],
        "low": [r[3] for r in rows],
        "volume": [r[4] or 0.0 for r in rows],
        "trades": [r[5] or 0 for r in rows],
        "taker_buy": [r[6] for r in rows],
    }


def cost_bps(position_usdc: float, summary_path: Path = None):
    path = summary_path or DAY1_SUMMARY
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text())
        for r in d.get("summary", []):
            if abs(r["position_usdc"] - position_usdc) < 1e-9:
                net = r.get("network_median_usdc")
                return net / position_usdc * 10_000 if net else None
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return None


# ── Sinais ───────────────────────────────────────────────────────────
# Cada um devolve lista alinhada aos candles, com None onde não há dado.

def sig_momentum(d, w):
    c, n = d["close"], len(d["close"])
    out = [None] * n
    for i in range(w, n):
        if c[i - w] > 0:
            out[i] = c[i] / c[i - w] - 1.0
    return out


def sig_order_flow(d, w):
    """
    Desequilíbrio de fluxo agressor: (compra - venda) / total na janela.

    Natureza diferente de preço — mede quem tinha PRESSA, não onde o preço
    esteve. Se houver informação em fluxo, ela não aparece em momentum.
    """
    vol, tb, n = d["volume"], d["taker_buy"], len(d["close"])
    if all(x is None for x in tb):
        return None
    out = [None] * n
    acc_v = acc_b = 0.0
    for i in range(n):
        v = vol[i] or 0.0
        b = tb[i] or 0.0
        acc_v += v
        acc_b += b
        if i >= w:
            acc_v -= vol[i - w] or 0.0
            acc_b -= tb[i - w] or 0.0
        if i >= w and acc_v > 0:
            out[i] = (2 * acc_b - acc_v) / acc_v   # +1 so compra, -1 so venda
    return out


def sig_volume_surge(d, w):
    """Volume da janela contra a média longa. Atividade anormal."""
    vol, n = d["volume"], len(d["close"])
    long_w = w * 10
    out = [None] * n
    short = long = 0.0
    for i in range(n):
        v = vol[i] or 0.0
        short += v
        long += v
        if i >= w:
            short -= vol[i - w] or 0.0
        if i >= long_w:
            long -= vol[i - long_w] or 0.0
        if i >= long_w and long > 0:
            out[i] = (short / w) / (long / long_w) - 1.0
    return out


def sig_range_position(d, w):
    """Onde o fechamento está na faixa alta-baixa da janela. 0=fundo, 1=topo."""
    c, hi, lo, n = d["close"], d["high"], d["low"], len(d["close"])
    out = [None] * n
    for i in range(w, n):
        h = max(hi[i - w + 1:i + 1])
        l = min(lo[i - w + 1:i + 1])
        if h > l:
            out[i] = (c[i] - l) / (h - l)
    return out


def sig_volatility(d, w):
    """Volatilidade realizada da janela."""
    c, n = d["close"], len(d["close"])
    rets = [0.0] * n
    for i in range(1, n):
        if c[i - 1] > 0:
            rets[i] = c[i] / c[i - 1] - 1.0
    out = [None] * n
    for i in range(w, n):
        seg = rets[i - w + 1:i + 1]
        out[i] = statistics.pstdev(seg) if len(seg) > 1 else None
    return out


def sig_acceleration(d, w):
    """Momentum da janela menos o da janela dupla. Mudança de ritmo."""
    m1, m2 = sig_momentum(d, w), sig_momentum(d, w * 2)
    return [None if (a is None or b is None) else a - b for a, b in zip(m1, m2)]


def build_signals(d):
    sigs = {}
    for w in WINDOWS:
        sigs[f"momentum_{w}"] = sig_momentum(d, w)
        sigs[f"range_pos_{w}"] = sig_range_position(d, w)
        sigs[f"volatility_{w}"] = sig_volatility(d, w)
        sigs[f"accel_{w}"] = sig_acceleration(d, w)
        sigs[f"vol_surge_{w}"] = sig_volume_surge(d, w)
        f = sig_order_flow(d, w)
        if f is not None:
            sigs[f"order_flow_{w}"] = f
    return {k: v for k, v in sigs.items() if v is not None}


# ── Avaliação ────────────────────────────────────────────────────────

def forward_return_bps(closes, i, h):
    """
    Retorno futuro a partir do candle SEGUINTE ao sinal.

    Comecar em closes[i] cria correlacao MECANICA: o sinal e calculado com
    closes[i], e o retorno closes[i+h]/closes[i] carrega o mesmo closes[i]
    invertido. Todo fechamento tem ruido de microestrutura (o quique entre
    compra e venda), que entra positivo no sinal e negativo no retorno. Isso
    fabrica reversao a media que nao existe.

    Testado: com a versao errada, um passeio aleatorio puro produzia dois
    sinais "confirmados" com |t| acima de 6. Comecando em i+1 o efeito some.

    Tambem e o realismo correto — nao se negocia o fechamento que acabou de
    ser observado.
    """
    a = i + 1
    b = a + h
    if b >= len(closes) or a >= len(closes) or closes[a] <= 0:
        return None
    return (closes[b] / closes[a] - 1.0) * 10_000


def decile_spread(sig, closes, h, lo, hi):
    """
    Spread entre o decil de cima e o de baixo do sinal.

    Passo h: amostras NAO SOBREPOSTAS. Com passo 1, observacoes vizinhas
    compartilham h-1 minutos de retorno futuro e o t sai inflado varias
    vezes. Isso custa amostra e compra honestidade.
    """
    pairs = []
    for i in range(lo, hi, h):
        s = sig[i]
        if s is None:
            continue
        r = forward_return_bps(closes, i, h)
        if r is None:
            continue
        pairs.append((s, r))

    if len(pairs) < DECILES * 10:
        return None

    pairs.sort(key=lambda p: p[0])
    k = len(pairs) // DECILES
    bot = [p[1] for p in pairs[:k]]
    top = [p[1] for p in pairs[-k:]]

    m_top, m_bot = statistics.fmean(top), statistics.fmean(bot)
    spread = m_top - m_bot
    if len(top) < 3 or len(bot) < 3:
        return None
    se = math.sqrt(statistics.variance(top) / len(top)
                   + statistics.variance(bot) / len(bot))
    return {
        "n": len(pairs), "decile_n": k,
        "top_bps": m_top, "bottom_bps": m_bot,
        "spread_bps": spread,
        "se": se,
        "t": spread / se if se > 0 else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--symbol", default="SOLUSDC")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--horizons", default=",".join(str(h) for h in HORIZONS))
    ap.add_argument("--position", type=float, default=5.0)
    ap.add_argument("--day1", type=Path, default=None)
    args = ap.parse_args()

    horizons = [int(x) for x in args.horizons.split(",")]
    d = load(args.db, args.symbol, args.interval)
    closes = d["close"]
    split = len(closes) // 2
    sigs = build_signals(d)
    c5 = cost_bps(args.position, args.day1)

    print("=" * 78)
    print("  TAIOS-SWARM — VARREDURA DE PODER PREDITIVO")
    print("=" * 78)
    print(f"  serie     : {args.symbol} {args.interval}, {len(closes):,} candles")
    print(f"  sinais    : {len(sigs)}")
    print(f"  horizontes: {horizons} minutos")
    print(f"  split     : treino 0..{split:,} | validacao {split:,}..{len(closes):,}")
    n_tests = len(sigs) * len(horizons)
    bonf = 0.05 / n_tests
    t_bonf = 3.0 if n_tests < 100 else 3.5   # aproximacao do t critico
    print(f"  testes    : {n_tests} pares (sinal, horizonte)")
    print(f"  Bonferroni: alpha {bonf:.5f} -> |t| acima de ~{t_bonf}")
    if c5:
        print(f"  custo     : {c5:.2f} bps por round trip em US${args.position:g}")
    print()
    print("  Spread positivo = decil alto do sinal sobe mais (entrada comprada).")
    print("  Spread negativo = decil alto CAI (entrada vendida). Os dois servem.")
    print()

    results = []
    for name, sig in sorted(sigs.items()):
        for h in horizons:
            tr = decile_spread(sig, closes, h, 0, split)
            va = decile_spread(sig, closes, h, split, len(closes))
            if tr and va:
                results.append({"signal": name, "horizon": h, "train": tr, "val": va})

    if not results:
        print("  Nenhum par com amostra suficiente.")
        return

    # Ordena pelo |t| do TREINO. A validacao nunca entra na selecao.
    results.sort(key=lambda r: -abs(r["train"]["t"] or 0))

    print("  TOP 15 POR |t| NO TREINO (validacao nao participa da selecao)")
    print("  " + "-" * 74)
    print(f"  {'SINAL':>16} {'H':>4} | {'TREINO':>18} | {'VALIDACAO':>18} | {'n':>5}")
    print(f"  {'':>16} {'min':>4} | {'spread bps':>10} {'t':>7} | "
          f"{'spread bps':>10} {'t':>7} | {'':>5}")
    print("  " + "-" * 74)
    for r in results[:15]:
        t, v = r["train"], r["val"]
        # Confirma: mesmo sinal, mesma direcao, |t| relevante na validacao
        same_sign = (t["spread_bps"] > 0) == (v["spread_bps"] > 0)
        mark = ""
        if same_sign and abs(v["t"] or 0) > 2:
            mark = "  <-- CONFIRMA"
        print(f"  {r['signal']:>16} {r['horizon']:>4} | "
              f"{t['spread_bps']:>10.2f} {t['t']:>7.2f} | "
              f"{v['spread_bps']:>10.2f} {v['t']:>7.2f} | {v['n']:>5}{mark}")

    # ── Confirmados ──
    confirmed = [r for r in results
                 if (r["train"]["spread_bps"] > 0) == (r["val"]["spread_bps"] > 0)
                 and abs(r["train"]["t"] or 0) > t_bonf
                 and abs(r["val"]["t"] or 0) > 2]

    print()
    print("=" * 78)
    print("  RESULTADO")
    print("=" * 78)
    print()
    print(f"  pares testados                 : {n_tests}")
    print(f"  confirmados (treino Bonferroni + validacao mesma direcao, |t|>2): "
          f"{len(confirmed)}")
    print(f"  esperado por acaso na validacao: ~{n_tests * 0.05:.1f}")

    if confirmed:
        print()
        print("  SINAIS CONFIRMADOS")
        print("  " + "-" * 74)
        for r in confirmed:
            v = r["val"]
            gross = abs(v["spread_bps"])
            print(f"    {r['signal']} @ {r['horizon']}min")
            print(f"      spread bruto na validacao : {v['spread_bps']:+.2f} bps "
                  f"(t={v['t']:.2f}, n={v['n']})")
            print(f"      direcao                   : "
                  + ("decil alto SOBE -> comprar o topo"
                     if v["spread_bps"] > 0 else "decil alto CAI -> vender o topo"))
            if c5:
                # metade do spread e o que uma perna captura
                per_leg = gross / 2
                print(f"      por perna (bruto)         : {per_leg:.2f} bps")
                for size in (5, 25, 50, 250):
                    cs = c5 * args.position / size
                    net = per_leg - cs
                    flag = "LUCRO" if net > 0 else "perda"
                    print(f"        US${size:>4}: custo {cs:>6.2f} bps -> "
                          f"liquido {net:>+7.2f} bps  {flag}")
        print()
        print("  ATENCAO 1: spread de decil e um TETO. Ele assume entrada exata no")
        print("  decil extremo capturando o retorno medio dele. Estrategia real erra")
        print("  o timing e nao pega o decil inteiro.")
        print()
        print("  ATENCAO 2: reversao em horizonte curto (5-15 min) costuma ser quique")
        print("  entre compra e venda, nao previsao. O custo aqui NAO inclui spread,")
        print("  entao um sinal desses pode estar 'lucrando' exatamente o spread que")
        print("  nao esta sendo cobrado. Desconfie de reversao curta ate medir spread.")
    else:
        print()
        print("  Nenhum sinal confirmou. O decil extremo nao prediz o retorno")
        print("  futuro de forma estavel entre as duas metades da serie.")

    out = ROOT / "data" / "signal_scan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "symbol": args.symbol, "interval": args.interval,
        "tests": n_tests, "confirmed": len(confirmed),
        "results": results[:50],
    }, indent=2), "utf-8")
    print(f"\n  Detalhe: {out}")


if __name__ == "__main__":
    main()
