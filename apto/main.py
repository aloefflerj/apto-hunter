"""Ponto de entrada: python -m apto.main"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from . import core
from .core import BRT, Bairros, Store
from .notify import send_telegram

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("apto")

DEFAULT_PPM2 = 4200.0  # R$/m² de referência até haver dados suficientes
ACTIVE_DAYS = 10       # some da página se não aparecer em nenhuma busca por X dias


def load_cfg(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def ingest(store: Store, items: list[dict], run_at: str):
    """Junta anúncios ao histórico, agrupando o mesmo imóvel visto em sites diferentes."""
    L = store.listings
    url_index = {lk["url"]: cid for cid, c in L.items() for lk in c["links"]}
    key_index = {c["key"]: cid for cid, c in L.items()}
    touched = set()
    for it in items:
        cid = url_index.get(it["url"])
        key = core.cluster_key(it)
        if cid is None:
            cid = key_index.get(key)
        if cid is None:
            cid = core.listing_id(it)
            L[cid] = {"id": cid, "key": key, "first_seen": run_at, "links": [], "price_history": []}
            key_index[key] = cid
        c = L[cid]
        if not any(lk["url"] == it["url"] for lk in c["links"]):
            c["links"].append({"source": it["source"], "url": it["url"]})
            url_index[it["url"]] = cid
        # preço: registra mudanças
        if c.get("price") and it["price"] and it["price"] != c["price"] and cid not in touched:
            c["price_history"].append({"at": run_at, "from": c["price"], "to": it["price"]})
        # mescla campos (não sobrescreve informação boa com vazio)
        for k, v in it.items():
            if k in ("source", "url"):
                continue
            if v not in (None, "", []) or k not in c:
                if k == "gas" and v is None and c.get("gas") is not None:
                    continue
                if k == "detailed" and c.get("detailed"):
                    continue
                c[k] = v
        c["last_seen"] = run_at
        touched.add(cid)
    return touched


def compute_median(store: Store, cfg):
    cutoff = (datetime.now(BRT) - timedelta(days=30)).isoformat()
    vals = [c["price"] / c["area"] for c in store.listings.values()
            if c.get("passes") and c.get("area") and c.get("price") and c.get("last_seen", "") >= cutoff]
    if len(vals) < 8:
        return DEFAULT_PPM2, len(vals)
    return statistics.median(vals), len(vals)


def evaluate(store: Store, cfg, bairros: Bairros):
    # 1ª passada: filtros básicos (para calcular a mediana)
    for c in store.listings.values():
        c["reject"] = core.basic_filter(c, cfg, bairros)
        c["passes"] = c["reject"] is None
    med, n = compute_median(store, cfg)
    for c in store.listings.values():
        if c["passes"] and not core.exception_ok(c, cfg, bairros, med):
            c["passes"], c["reject"] = False, "acima do teto sem desconto suficiente"
        c["exception"] = bool(c["passes"] and c["price"] > cfg["preco"]["maximo"])
        c["ppm2"] = round(core.ppm2(c)) if core.ppm2(c) else None
        c["score"], c["score_parts"] = core.score(c, cfg, bairros, med) if c["passes"] else (0, {})
        c["tier"] = bairros.tier.get(c.get("bairro"), None)
        ph = c.get("price_history") or []
        c["price_drop"] = bool(ph and ph[-1]["to"] < ph[-1]["from"])
    return med, n


def write_site(store: Store, cfg, med, n_med, run_at, stats):
    cutoff = (datetime.now(BRT) - timedelta(days=ACTIVE_DAYS)).isoformat()
    rows = [c for c in store.listings.values() if c["passes"] and c.get("last_seen", "") >= cutoff]
    rows.sort(key=lambda c: c["score"], reverse=True)
    keep = ["id", "title", "img", "price", "area", "rooms", "parking", "condo", "iptu", "bairro", "tier",
            "gas", "semi_furnished", "exception", "ppm2", "score", "score_parts", "links", "first_seen",
            "last_seen", "price_history", "price_drop", "snippet", "address"]
    out = {
        "updated_at": run_at,
        "median_ppm2": round(med),
        "median_sample": n_med,
        "stats": stats,
        "rules": {"preco_max": cfg["preco"]["maximo"], "preco_excecao": cfg["preco"]["maximo_excecao"],
                  "area_min": cfg["area_minima"], "quartos_min": cfg["quartos_minimo"]},
        "listings": [{k: c.get(k) for k in keep} for c in rows],
    }
    dst = ROOT / "docs" / "data.json"
    dst.parent.mkdir(exist_ok=True)
    dst.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="rodar só algumas fontes (zap vivareal olx quintoandar imovelweb chavesnamao)")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--headed", action="store_true", help="abre o navegador visível (debug)")
    ap.add_argument("--raw-file", help="usa um JSON de anúncios crus em vez de raspar (testes)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_cfg(ROOT / "config.yaml")
    bairros = Bairros(cfg)
    store = Store(ROOT / "data" / "listings.json")
    run_at = core.now_iso()

    # ---- coleta
    if args.raw_file:
        raw = json.loads(Path(args.raw_file).read_text(encoding="utf-8"))
        stats = {"arquivo": len(raw)}
        session = None
    else:
        from .sources import fetch_details, open_session, scrape_all
        session = open_session(headless=not args.headed)
        s = session.__enter__()
        raw, stats = scrape_all(s, cfg, bairros.all(), args.only)
    log.info("coletados: %s", stats)

    parsed, rejects = [], {}
    for r in raw:
        it = core.parse_raw(r, bairros)
        if it is None:
            continue
        why = core.basic_filter(it, cfg, bairros)
        if why in ("sem link", "sem preço", "fora da região"):
            rejects[why] = rejects.get(why, 0) + 1
            continue
        parsed.append(it)
    log.info("após parse: %d (descartes: %s)", len(parsed), rejects)

    before = set(store.listings)
    ingest(store, parsed, run_at)
    new_ids = set(store.listings) - before

    # ---- detalhes (gás / mobiliado) dos novos que passam no filtro
    evaluate(store, cfg, bairros)
    if session is not None:
        todo = [c for cid, c in store.listings.items()
                if c["passes"] and not c.get("detailed")]
        todo.sort(key=lambda c: (c["id"] not in new_ids, -c["score"]))
        todo = todo[: cfg.get("detalhes_por_execucao", 30)]
        urls = {c["links"][0]["url"]: c for c in todo}
        log.info("lendo detalhes de %d anúncios", len(urls))
        try:
            texts = fetch_details(s, list(urls))
        except Exception as e:  # noqa: BLE001
            log.warning("detalhes falharam: %s", e)
            texts = {}
        for u, txt in texts.items():
            core.apply_description(urls[u], txt)
        session.__exit__(None, None, None)

    med, n_med = evaluate(store, cfg, bairros)

    # ---- novidades
    news = [store.listings[i] for i in new_ids if store.listings[i]["passes"]]
    drops = [c for c in store.listings.values()
             if c["passes"] and c["price_drop"] and c["price_history"][-1]["at"] == run_at and c["id"] not in new_ids]
    news.sort(key=lambda c: c["score"], reverse=True)

    rows = write_site(store, cfg, med, n_med, run_at, stats)
    store.data["runs"] = (store.data.get("runs") or [])[-60:] + [{
        "at": run_at, "stats": stats, "new": len(news), "drops": len(drops), "active": len(rows)}]
    store.save()
    log.info("novos: %d | baixaram preço: %d | ativos na página: %d | mediana R$/m²: %.0f (%d)",
             len(news), len(drops), len(rows), med, n_med)

    if not args.no_telegram:
        send_telegram(cfg, news, drops, rows, stats)


if __name__ == "__main__":
    main()
