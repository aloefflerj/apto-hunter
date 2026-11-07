"""Envio do resumo diário pelo Telegram."""
from __future__ import annotations

import html
import logging
import os

import requests

log = logging.getLogger(__name__)


def brl(v):
    return "R$ " + f"{int(v):,}".replace(",", ".") if v else "—"


ACC = {"sao joao": "São João", "petropolis": "Petrópolis", "higienopolis": "Higienópolis", "passo da areia": "Passo d'Areia/IAPI",
       "jardim sao pedro": "Jardim São Pedro", "jardim lindoia": "Jardim Lindóia", "tres figueiras": "Três Figueiras",
       "independencia": "Independência", "jardim itu sabara": "Jardim Itu Sabará", "sao geraldo": "São Geraldo",
       "humaita": "Humaitá", "santa cecilia": "Santa Cecília", "jardim botanico": "Jardim Botânico", "chacara das pedras": "Chácara das Pedras"}


def title_bairro(b):
    if b in ACC:
        return ACC[b]
    small = {"da", "de", "do", "das", "dos"}
    return " ".join(w if w in small else w.capitalize() for w in (b or "").split())


def line(c, i):
    bits = [f"{int(c['area'])} m²" if c.get("area") else None,
            f"{c['rooms']} q" if c.get("rooms") else None,
            f"{c['parking']} vaga" if c.get("parking") else None,
            f"cond {brl(c['condo'])}" if c.get("condo") else None,
            "🔥 gás" if c.get("gas") else None]
    extra = " · ".join(b for b in bits if b)
    flags = []
    if c.get("exception"):
        flags.append("acima de 250k, mas muito barato/m²")
    if c.get("price_drop"):
        h = c["price_history"][-1]
        flags.append(f"baixou de {brl(h['from'])}")
    flag = f"\n   <i>{html.escape('; '.join(flags))}</i>" if flags else ""
    url = c["links"][0]["url"]
    src = ", ".join(sorted({lk["source"] for lk in c["links"]}))
    return (f"<b>{i}. {brl(c['price'])}</b> — {html.escape(title_bairro(c['bairro']))} · nota <b>{c['score']:.0f}</b>\n"
            f"   {html.escape(extra)} · {brl(c.get('ppm2'))}/m²{flag}\n"
            f"   <a href=\"{html.escape(url)}\">ver no {html.escape(src)}</a>")


def send_telegram(cfg, news, drops, rows, stats):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.warning("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID não definidos — pulando envio")
        return
    tg = cfg.get("telegram", {})
    maxn = tg.get("max_itens", 10)
    site = cfg.get("site_url", "")
    parts = []
    if news:
        parts.append(f"🏠 <b>{len(news)} apartamento(s) novo(s)</b> dentro das suas regras hoje:\n")
        parts += [line(c, i + 1) for i, c in enumerate(news[:maxn])]
        if len(news) > maxn:
            parts.append(f"\n…e mais {len(news) - maxn} na página.")
    if drops:
        parts.append(f"\n📉 <b>{len(drops)} baixaram de preço</b>:")
        parts += [line(c, i + 1) for i, c in enumerate(drops[:5])]
    if not parts:
        if not tg.get("enviar_quando_vazio", True):
            return
        parts.append("Nenhum apartamento novo hoje dentro das regras.")
    best = rows[0] if rows else None
    if best:
        parts.append(f"\n🥇 Melhor da lista agora: {brl(best['price'])} em {html.escape(title_bairro(best['bairro']))} (nota {best['score']:.0f})")
    empty = [k for k, v in (stats or {}).items() if v == 0]
    if empty:
        parts.append(f"\n⚠️ Sem resultados de: {', '.join(empty)} (possível bloqueio)")
    if site:
        parts.append(f"\n📋 <a href=\"{html.escape(site)}\">Ranking completo ({len(rows)} imóveis)</a>")

    text = "\n".join(parts)
    # Telegram limita a 4096 caracteres
    chunks, cur = [], ""
    for p in text.split("\n"):
        if len(cur) + len(p) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += p + "\n"
    chunks.append(cur)
    for ch in chunks:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": ch, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=30)
        if not r.ok:
            log.error("telegram: %s %s", r.status_code, r.text[:300])
