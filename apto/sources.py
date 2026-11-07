"""Fontes de anúncios.

Cada fonte roda dentro de um navegador real (Playwright/Chromium) para passar
pelas proteções anti-robô. A extração acontece com JavaScript dentro da página:
o HTML de cada página de resultados é baixado com fetch() no próprio site
(mesmos cookies) e lido com DOMParser. Se isso falhar, o navegador abre a
página normalmente e extrai do DOM renderizado.

Cada extrator devolve dicts "crus":
  {source, url, title, text, img, price?, area?, rooms?, parking?, condo?, iptu?,
   bairro?, furnished?, gas?, description?}
O parse.py completa o que faltar a partir do texto.
"""
from __future__ import annotations

import logging
import random
import time
from urllib.parse import quote

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Extratores JS (recebem `doc`, um Document; devolvem lista de objetos)
# ----------------------------------------------------------------------------

JS_ZAP_LIKE = r"""
(doc) => { const T = el => { const w = (el.ownerDocument || document).createTreeWalker(el, NodeFilter.SHOW_TEXT); const out = []; let n; while ((n = w.nextNode())) out.push(n.nodeValue); return out.join(' ').replace(/\s+/g, ' ').trim(); }; return [...doc.querySelectorAll('[data-cy="rp-property-cd"]')].map(li => {
  const a = li.querySelector('a[href]');
  const href = a ? new URL(a.getAttribute('href'), location.origin).href.split('?')[0] : '';
  const img = li.querySelector('img');
  return {
    url: href.includes('/imovel/') ? href : '',
    title: a ? (a.getAttribute('title') || '') : '',
    text: T(li),
    img: img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : ''
  };
}); }
"""

JS_OLX = r"""
(doc) => { const T = el => { const w = (el.ownerDocument || document).createTreeWalker(el, NodeFilter.SHOW_TEXT); const out = []; let n; while ((n = w.nextNode())) out.push(n.nodeValue); return out.join(' ').replace(/\s+/g, ' ').trim(); }; return [...doc.querySelectorAll('section.olx-adcard')].map(c => {
  const a = c.querySelector('a[data-testid="adcard-link"]') || c.querySelector('a[href]');
  const q = s => (c.querySelector(s)?.textContent || '').replace(/\s+/g, ' ').trim();
  const det = [...c.querySelectorAll('.olx-adcard__detail')].map(d => d.getAttribute('aria-label') || d.textContent).join(' | ');
  const src = c.querySelector('picture source')?.getAttribute('srcset') || c.querySelector('img')?.getAttribute('src') || '';
  return {
    url: a ? a.href || a.getAttribute('href') : '',
    title: q('.olx-adcard__title'),
    price_text: q('.olx-adcard__price'),
    extra: [...c.querySelectorAll('.olx-adcard__price-info')].map(x => x.textContent).join(' | '),
    location: q('.olx-adcard__location'),
    date: q('.olx-adcard__date'),
    details: det,
    text: T(c),
    img: src.split(' ')[0]
  };
}); }
"""

JS_IMOVELWEB = r"""
(doc) => { const T = el => { const w = (el.ownerDocument || document).createTreeWalker(el, NodeFilter.SHOW_TEXT); const out = []; let n; while ((n = w.nextNode())) out.push(n.nodeValue); return out.join(' ').replace(/\s+/g, ' ').trim(); }; return [...doc.querySelectorAll('[data-to-posting]')].map(c => {
  const img = c.querySelector('img');
  return {
    url: new URL(c.getAttribute('data-to-posting'), 'https://www.imovelweb.com.br').href.split('?')[0],
    title: c.querySelector('h2, h3')?.textContent?.trim() || '',
    text: T(c),
    img: img ? (img.getAttribute('src') || img.getAttribute('data-flickity-lazyload') || '') : ''
  };
}); }
"""

JS_CHAVES = r"""
(doc) => {
  const T = el => { const w = (el.ownerDocument || document).createTreeWalker(el, NodeFilter.SHOW_TEXT); const out = []; let n; while ((n = w.nextNode())) out.push(n.nodeValue); return out.join(' ').replace(/\s+/g, ' ').trim(); };
  const seen = new Set();
  return [...doc.querySelectorAll('a[href*="/imovel/"]')].filter(a => {
    const h = a.href || a.getAttribute('href');
    if (seen.has(h)) return false; seen.add(h); return true;
  }).map(a => {
    const card = a.closest('article, li, div[class*="card"]') || a;
    const img = card.querySelector('img');
    return {
      url: new URL(a.getAttribute('href'), 'https://www.chavesnamao.com.br').href.split('?')[0],
      title: (a.getAttribute('title') || card.querySelector('h2,h3')?.textContent || '').trim(),
      text: T(card),
      img: img ? (img.getAttribute('data-src') || img.getAttribute('src') || '') : ''
    };
  });
}
"""

# Busca HTML via fetch no contexto da página e aplica um extrator
JS_FETCH_AND_EXTRACT = r"""
async ([url, extractorSrc]) => {
  const extractor = eval(extractorSrc);
  const r = await fetch(url, {credentials: 'include'});
  const html = await r.text();
  const doc = new DOMParser().parseFromString(html, 'text/html');
  return {status: r.status, items: extractor(doc)};
}
"""

JS_DETAIL_TEXT = r"""
async (url) => {
  try {
    const r = await fetch(url, {credentials: 'include'});
    const html = await r.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    doc.querySelectorAll('script,style,noscript,svg,header,footer,nav').forEach(e => e.remove());
    const meta = doc.querySelector('meta[name="description"]')?.content || '';
    const txt = (doc.body?.textContent || '').replace(/\s+/g, ' ');
    return {status: r.status, text: (meta + ' ' + txt).slice(0, 20000)};
  } catch (e) { return {status: 0, text: ''}; }
}
"""

# QuintoAndar: API pública usada pelo próprio site (chamada de dentro da página)
JS_QUINTOANDAR = r"""
async ([offset, size, maxPrice, minArea, minRooms, vp]) => {
  const body = {
    context: {mapShowing: true, listShowing: true, numPhotos: 3, isSSR: false},
    filters: {
      businessContext: 'SALE', blocklist: [], selectedHouses: [],
      location: {coordinate: {lat: (vp.north + vp.south) / 2, lng: (vp.east + vp.west) / 2},
                 viewport: vp, neighborhoods: [], countryCode: 'BR'},
      priceRange: [{costType: 'SALE_PRICE', range: {min: 0, max: maxPrice}}],
      specialConditions: [], excludedSpecialConditions: [],
      houseSpecs: {area: {range: {min: minArea}}, houseTypes: ['APARTMENT'], amenities: [], installations: [],
                   bathrooms: {range: {}}, bedrooms: {range: {min: minRooms}}, parkingSpace: {range: {}}, suites: {range: {}}},
      availability: 'ANY', occupancy: 'ANY', partnerIds: [], categories: []
    },
    sorting: {criteria: 'MOST_RECENT', order: 'DESC'},
    pagination: {pageSize: size, offset: offset},
    slug: 'porto-alegre-rs-brasil',
    fields: ['id','coverImage','salePrice','condoPrice','iptuPrice','area','bedrooms','parkingSpaces','address',
             'regionName','neighbourhood','city','type','isFurnished','amenities','installations','listingTags',
             'shortSaleDescription'],
    locationDescriptions: [{description: 'porto-alegre-rs-brasil'}]
  };
  const r = await fetch('https://apigw.prod.quintoandar.com.br/house-listing-search/v2/search/list',
    {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  return (j.hits?.hits || []).map(h => h._source);
}
"""

# ----------------------------------------------------------------------------
# URLs de busca
# ----------------------------------------------------------------------------

ZAP_SLUGS = {  # slug do ZAP por bairro (o ZAP usa abreviações)
    "sao joao": "s-joao", "passo da areia": "passo-da-areia", "petropolis": "petropolis",
    "auxiliadora": "auxiliadora", "higienopolis": "higienopolis", "vila ipiranga": "vl-ipiranga",
    "cristo redentor": "cristo-redentor", "jardim lindoia": "jd-lindoia", "boa vista": "boa-vista",
    "santa maria goretti": "sta-maria-goretti", "floresta": "floresta", "navegantes": "navegantes",
    "jardim floresta": "jd-floresta", "tres figueiras": "tres-figueiras", "mont serrat": "mont-serrat",
    "bela vista": "bela-vista", "rio branco": "rio-branco", "moinhos de vento": "moinhos-de-vento",
    "independencia": "independencia", "jardim europa": "jd-europa", "jardim itu sabara": "jd-itu-sabara",
    "sao geraldo": "s-geraldo", "humaita": "humaita", "farrapos": "farrapos", "santa cecilia": "sta-cecilia",
    "jardim botanico": "jd-botanico", "santana": "santana",
}


def slugify(name: str) -> str:
    return name.replace(" ", "-")


def zap_urls(cfg, bairros):
    p = cfg["preco"]["maximo_excecao"]
    a = cfg["area_minima"]
    for b in bairros:
        slug = ZAP_SLUGS.get(b)
        if not slug:
            continue
        for page in range(1, cfg["paginas"]["zap"] + 1):
            yield (f"https://www.zapimoveis.com.br/venda/apartamentos/rs+porto-alegre++{slug}/"
                   f"?quartos=2,3,4&precoMaximo={p}&areaMinima={a}" + (f"&pagina={page}" if page > 1 else ""))


def vivareal_urls(cfg, bairros):
    p = cfg["preco"]["maximo_excecao"]
    a = cfg["area_minima"]
    for b in bairros:
        for page in range(1, cfg["paginas"]["vivareal"] + 1):
            yield (f"https://www.vivareal.com.br/venda/rio-grande-do-sul/porto-alegre/bairros/{slugify(b)}/apartamento_residencial/"
                   f"?quartos=2,3,4&precoMaximo={p}&areaMinima={a}" + (f"&pagina={page}" if page > 1 else ""))


def olx_urls(cfg):
    p = cfg["preco"]["maximo_excecao"]
    a = cfg["area_minima"]
    base = ("https://www.olx.com.br/imoveis/venda/apartamentos/estado-rs/regioes-de-porto-alegre-torres-e-santa-cruz-do-sul"
            f"?pe={p}&ros={cfg['quartos_minimo']}&ss={a}")
    for page in range(1, cfg["paginas"]["olx"] + 1):
        yield base + (f"&o={page}" if page > 1 else "")


def imovelweb_urls(cfg, bairros):
    p = cfg["preco"]["maximo_excecao"]
    for b in bairros:
        for page in range(1, cfg["paginas"]["imovelweb"] + 1):
            pg = f"-pagina-{page}" if page > 1 else ""
            yield (f"https://www.imovelweb.com.br/apartamentos-venda-{slugify(b)}-porto-alegre-mais-de-2-quartos-"
                   f"menos-{p}-reales{pg}.html")


def chaves_urls(cfg, bairros):
    p = cfg["preco"]["maximo_excecao"]
    a = cfg["area_minima"]
    for b in bairros:
        for page in range(1, cfg["paginas"]["chavesnamao"] + 1):
            yield (f"https://www.chavesnamao.com.br/apartamentos-a-venda/rs-porto-alegre/{slugify(b)}/"
                   f"?filtro=pmax:{p},amin:{a},qmin:{cfg['quartos_minimo']}" + (f"&pg={page}" if page > 1 else ""))


# Retângulo que cobre a região de interesse (Navegantes/Floresta → Petrópolis, Bela Vista → triângulo da Assis Brasil)
QA_VIEWPORT = {"north": -29.983, "south": -30.052, "east": -51.140, "west": -51.218}

# ----------------------------------------------------------------------------
# Execução
# ----------------------------------------------------------------------------


def _pause(a=1.2, b=3.0):
    time.sleep(random.uniform(a, b))


class Scraper:
    def __init__(self, page):
        self.page = page

    def warmup(self, url):
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            _pause(2.5, 4.5)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("warmup falhou %s: %s", url, e)
            return False

    def list_page(self, url, js_extractor, origin_check=None):
        """Tenta fetch+DOMParser; se vier vazio, navega e extrai do DOM."""
        try:
            res = self.page.evaluate(JS_FETCH_AND_EXTRACT, [url, js_extractor])
            if res["status"] == 404:
                return []
            if res["items"]:
                return res["items"]
        except Exception as e:  # noqa: BLE001
            log.info("fetch falhou (%s), tentando navegação: %s", url, e)
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if resp and resp.status == 404:
                return []
            _pause(2, 4)
            self.page.mouse.wheel(0, 4000)
            _pause(1, 2)
            return self.page.evaluate(f"({js_extractor})(document)")
        except Exception as e:  # noqa: BLE001
            log.warning("navegação falhou %s: %s", url, e)
            return []

    def detail_text(self, url):
        try:
            res = self.page.evaluate(JS_DETAIL_TEXT, url)
            return res.get("text", "")
        except Exception:  # noqa: BLE001
            return ""


def run_source(scraper, name, warm_url, urls, js, tag, max_empty_streak=2):
    out = []
    if not scraper.warmup(warm_url):
        return out
    # para cada "grupo" (bairro), parar paginação quando página vier vazia
    last_group, empty = None, 0
    for url in urls:
        group = url.split("?")[0].removesuffix(".html").rsplit("-pagina-", 1)[0]
        if group != last_group:
            last_group, empty = group, 0
        elif empty >= 1:
            continue
        items = scraper.list_page(url, js)
        log.info("[%s] %s -> %d", name, url, len(items))
        if not items:
            empty += 1
        for it in items:
            it["source"] = tag
            out.append(it)
        _pause()
    return out


from contextlib import contextmanager


@contextmanager
def open_session(headless=True):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page()
        try:
            yield Scraper(page)
        finally:
            ctx.close()
            browser.close()


def scrape_all(s: "Scraper", cfg, bairros, only=None):
    jobs = {
        "zap": lambda: run_source(s, "zap", "https://www.zapimoveis.com.br/venda/apartamentos/rs+porto-alegre/",
                                  zap_urls(cfg, bairros), JS_ZAP_LIKE, "ZAP"),
        "vivareal": lambda: run_source(s, "vivareal", "https://www.vivareal.com.br/venda/rio-grande-do-sul/porto-alegre/apartamento_residencial/",
                                       vivareal_urls(cfg, bairros), JS_ZAP_LIKE, "VivaReal"),
        "olx": lambda: run_source(s, "olx", "https://www.olx.com.br/imoveis/venda/apartamentos/estado-rs/regioes-de-porto-alegre-torres-e-santa-cruz-do-sul",
                                  olx_urls(cfg), JS_OLX, "OLX"),
        "imovelweb": lambda: run_source(s, "imovelweb", "https://www.imovelweb.com.br/apartamentos-venda-porto-alegre-rs.html",
                                        imovelweb_urls(cfg, bairros), JS_IMOVELWEB, "Imovelweb"),
        "chavesnamao": lambda: run_source(s, "chavesnamao", "https://www.chavesnamao.com.br/apartamentos-a-venda/rs-porto-alegre/",
                                          chaves_urls(cfg, bairros), JS_CHAVES, "Chaves na Mão"),
        "quintoandar": lambda: quintoandar(s, cfg),
    }
    results, stats = [], {}
    for name, job in jobs.items():
        if only and name not in only:
            continue
        try:
            got = job()
        except Exception as e:  # noqa: BLE001
            log.exception("fonte %s quebrou: %s", name, e)
            got = []
        stats[name] = len(got)
        results.extend(got)
    return results, stats


def fetch_details(s: "Scraper", urls):
    """Baixa o texto da página de cada anúncio (agrupando por site)."""
    from urllib.parse import urlparse

    by_origin = {}
    for u in urls:
        o = urlparse(u)
        by_origin.setdefault(f"{o.scheme}://{o.netloc}/", []).append(u)
    out = {}
    for origin, us in by_origin.items():
        if not s.warmup(origin):
            continue
        for u in us:
            out[u] = s.detail_text(u)
            _pause(0.8, 2.0)
    return out


def quintoandar(s: Scraper, cfg):
    out = []
    if not s.warmup("https://www.quintoandar.com.br/comprar/imovel/porto-alegre-rs-brasil/apartamento"):
        return out
    size = 60
    for i in range(cfg["paginas"]["quintoandar"]):
        try:
            hs = s.page.evaluate(JS_QUINTOANDAR, [i * size, size, cfg["preco"]["maximo_excecao"],
                                                  cfg["area_minima"], cfg["quartos_minimo"], QA_VIEWPORT])
        except Exception as e:  # noqa: BLE001
            log.warning("quintoandar falhou: %s", e)
            break
        log.info("[quintoandar] offset %d -> %d", i * size, len(hs))
        for h in hs:
            tokens = " ".join((h.get("amenities") or []) + (h.get("installations") or []))
            gas = None
            if "GAS" in tokens:
                gas = True
            elif "CHUVEIRO_ELETRICO" in tokens:
                gas = False
            cover = h.get("coverImage") or ""
            out.append({
                "source": "QuintoAndar",
                "url": f"https://www.quintoandar.com.br/imovel/{h['id']}/comprar",
                "title": f"Apartamento {h.get('area')} m², {h.get('bedrooms')} quartos — {h.get('address') or ''}",
                "text": h.get("shortSaleDescription") or "",
                "description": h.get("shortSaleDescription") or "",
                "img": f"https://www.quintoandar.com.br/img/med/{quote(cover)}" if cover else "",
                "price": h.get("salePrice"),
                "area": h.get("area"),
                "rooms": h.get("bedrooms"),
                "parking": h.get("parkingSpaces"),
                "condo": h.get("condoPrice"),
                "iptu": h.get("iptuPrice"),
                "bairro": h.get("neighbourhood") or h.get("regionName"),
                "address": h.get("address"),
                "furnished": bool(h.get("isFurnished")),
                "gas": gas,
                "detailed": True,
            })
        if len(hs) < size:
            break
        _pause()
    return out
