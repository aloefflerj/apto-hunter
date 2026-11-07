"""Normalização, filtros, nota e armazenamento."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

BRT = timezone(timedelta(hours=-3))


def now_iso():
    return datetime.now(BRT).isoformat(timespec="seconds")


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().replace("'", " ").replace("’", " ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def money(s: str) -> int | None:
    s = s.replace(".", "").replace(" ", "")
    s = s.split(",")[0]
    return int(s) if s.isdigit() else None


# ----------------------------------------------------------------------------
# Bairros
# ----------------------------------------------------------------------------

class Bairros:
    def __init__(self, cfg):
        self.tier = {}
        for t, names in cfg["bairros"].items():
            for n in names:
                self.tier[norm(n)] = int(t)
        self.alias = {norm(k): norm(v) for k, v in (cfg.get("aliases") or {}).items()}
        # nomes para busca em texto, do mais longo pro mais curto
        self.names = sorted(set(self.tier) | set(self.alias), key=len, reverse=True)

    def canon(self, raw: str | None) -> str | None:
        n = norm(raw)
        if not n:
            return None
        n = self.alias.get(n, n)
        return n if n in self.tier else None

    def find_in(self, text: str) -> str | None:
        """Procura "<bairro>, Porto Alegre" no texto; cai para qualquer menção."""
        t = norm(text)
        for m in re.finditer(r"([a-z0-9 ]{3,60}) porto alegre", t):
            chunk = m.group(1).strip()
            for name in self.names:
                if chunk.endswith(name):
                    return self.alias.get(name, name)
        return None

    def all(self):
        return list(self.tier)


# ----------------------------------------------------------------------------
# Parse
# ----------------------------------------------------------------------------

RE_MONEY = re.compile(r"R\$\s*([\d.]+(?:,\d{2})?)")
RE_AREA = re.compile(r"(\d{2,4}(?:[.,]\d+)?)\s*(?:-\s*\d{2,4}\s*)?m(?:²|2)\b", re.I)
RE_ROOMS = [re.compile(r"quantidade de quartos\s*(\d+)", re.I),
            re.compile(r"(\d+)\s*(?:quartos?|dormit[óo]rios?|dorms?\.?)\b", re.I)]
RE_PARK = [re.compile(r"vagas? de garagem\s*(\d+)", re.I),
           re.compile(r"(\d+)\s*vagas?\b", re.I)]
RE_CONDO = [re.compile(r"cond(?:\.|om[íi]nio)\s*R\$\s*([\d.]+)", re.I),
            re.compile(r"R\$\s*([\d.]+)\s*condom", re.I)]
RE_IPTU = re.compile(r"IPTU\s*R\$\s*([\d.]+)", re.I)

# só considera "mobiliado" quando se refere ao imóvel inteiro (não "cozinha mobiliada")
RE_FURNISHED = re.compile(
    r"(apartamento|apto|im[óo]vel|totalmente|todo|completamente|100%|vendido|fica|entregue|porteira fechada)"
    r"\s+(?:\w+\s+)?mobiliad[oa]|-mobiliado-|porteira fechada", re.I)
RE_NOT_FURNISHED = re.compile(r"(n[ãa]o|sem)\s+mob[íi]li", re.I)
RE_SEMI = re.compile(r"semi[- ]?mobiliad", re.I)
RE_GAS = re.compile(
    r"(aquec\w*|chuveiro|[áa]gua quente|boiler|passagem)[^.;\n]{0,35}\bg[áa]s\b"
    r"|\bg[áa]s\b[^.;\n]{0,15}(central|encanado|canalizado)"
    r"|\ba g[áa]s\b", re.I)
RE_ELECTRIC = re.compile(r"chuveiro el[ée]trico", re.I)
RE_AUCTION = re.compile(r"leil[ãa]o", re.I)


def first(regs, text):
    for r in regs if isinstance(regs, list) else [regs]:
        m = r.search(text)
        if m:
            return m.group(1)
    return None


def parse_raw(raw: dict, bairros: Bairros) -> dict | None:
    text = " ".join(filter(None, [raw.get("title"), raw.get("text"), raw.get("details"),
                                  raw.get("price_text"), raw.get("extra")]))
    it = {
        "source": raw["source"],
        "url": raw.get("url") or "",
        "title": (raw.get("title") or "").strip()[:160],
        "img": raw.get("img") or "",
        "address": raw.get("address") or "",
    }

    # preço
    price = raw.get("price")
    if price is None:
        src = raw.get("price_text") or text
        vals = [money(v) for v in RE_MONEY.findall(src)]
        vals = [v for v in vals if v and v >= 50000]
        price = max(vals) if vals else None
    it["price"] = int(price) if price else None

    # área
    area = raw.get("area")
    if area is None:
        m = RE_AREA.search(text)
        area = float(m.group(1).replace(",", ".")) if m else None
    it["area"] = float(area) if area else None

    rooms = raw.get("rooms")
    if rooms is None:
        r = first(RE_ROOMS, text)
        rooms = int(r) if r else None
    it["rooms"] = rooms

    park = raw.get("parking")
    if park is None:
        r = first(RE_PARK, text)
        park = int(r) if r else None
    it["parking"] = park

    condo = raw.get("condo")
    if condo is None:
        c = first(RE_CONDO, text)
        condo = money(c) if c else None
    it["condo"] = condo or None

    iptu = raw.get("iptu")
    if iptu is None:
        m = RE_IPTU.search(text)
        iptu = money(m.group(1)) if m else None
    it["iptu"] = iptu or None

    # bairro
    b = None
    if raw.get("bairro"):
        b = bairros.canon(raw["bairro"])
        if b is None:
            it["bairro_raw"] = raw["bairro"]
    elif raw.get("location"):  # OLX: "Porto Alegre, Auxiliadora"
        loc = raw["location"]
        if "porto alegre" not in norm(loc):
            return None
        b = bairros.canon(loc.split(",")[-1])
        it["bairro_raw"] = loc.split(",")[-1].strip()
    else:
        b = bairros.find_in(text)
    it["bairro"] = b

    # características
    desc = raw.get("description") or ""
    full = text + " " + desc + " " + it["url"]
    it["furnished"] = raw.get("furnished")
    if it["furnished"] is None:
        it["furnished"] = bool(RE_FURNISHED.search(full)) and not RE_NOT_FURNISHED.search(full)
    it["semi_furnished"] = bool(RE_SEMI.search(full))
    gas = raw.get("gas")
    if gas is None:
        if RE_GAS.search(full):
            gas = True
        elif RE_ELECTRIC.search(full):
            gas = False
    it["gas"] = gas
    it["auction"] = bool(RE_AUCTION.search(text))
    it["detailed"] = bool(raw.get("detailed"))
    it["snippet"] = desc[:400]
    return it


def apply_description(it: dict, desc: str):
    """Reaplica detecções com a descrição completa do anúncio."""
    if not desc:
        return
    if it.get("gas") is None:
        if RE_GAS.search(desc):
            it["gas"] = True
        elif RE_ELECTRIC.search(desc):
            it["gas"] = False
    if RE_FURNISHED.search(desc) and not RE_NOT_FURNISHED.search(desc) and not RE_SEMI.search(desc):
        it["furnished"] = True
    if RE_SEMI.search(desc):
        it["semi_furnished"] = True
    if it.get("parking") is None:
        r = first(RE_PARK, desc)
        it["parking"] = int(r) if r else None
    if it.get("condo") is None:
        c = first(RE_CONDO, desc)
        it["condo"] = money(c) if c else None
    if not it.get("snippet"):
        it["snippet"] = desc[:300]
    it["detailed"] = True


# ----------------------------------------------------------------------------
# Filtros e nota
# ----------------------------------------------------------------------------

def basic_filter(it, cfg, bairros: Bairros):
    """Devolve motivo de descarte ou None."""
    if not (it.get("url") or it.get("links")):
        return "sem link"
    if not it["price"]:
        return "sem preço"
    if it["price"] > cfg["preco"]["maximo_excecao"]:
        return "caro"
    if it["area"] is not None and it["area"] < cfg["area_minima"]:
        return "pequeno"
    if it["rooms"] is not None and it["rooms"] < cfg["quartos_minimo"]:
        return "poucos quartos"
    if not it["bairro"]:
        return "fora da região"
    if cfg["excluir"].get("mobiliado") and it["furnished"] and not it["semi_furnished"]:
        return "mobiliado"
    if cfg["excluir"].get("leilao") and it["auction"]:
        return "leilão"
    return None


def ppm2(it):
    return it["price"] / it["area"] if it.get("price") and it.get("area") else None


def score(it, cfg, bairros: Bairros, median_ppm2: float):
    """Nota 0-100. Quanto maior, melhor a oportunidade."""
    pts = {}
    v = ppm2(it)
    # 1) preço por m² vs mediana da região (até 40)
    if v:
        ratio = v / median_ppm2
        pts["preco_m2"] = max(0.0, min(40.0, (1.25 - ratio) / 0.55 * 40))  # 0.70×med = 40, 1.25×med = 0
    else:
        pts["preco_m2"] = 10.0
    # 2) preço absoluto (até 10) — mais barato = mais folga no financiamento
    pmax = cfg["preco"]["maximo"]
    pts["preco"] = max(0.0, min(10.0, (pmax - it["price"]) / (pmax * 0.3) * 10))
    # 3) localização (até 20)
    tier = bairros.tier.get(it["bairro"], 3)
    pts["local"] = {1: 20, 2: 13, 3: 5}[tier]
    # 4) área (até 8)
    if it.get("area"):
        pts["area"] = max(0.0, min(8.0, (it["area"] - cfg["area_minima"]) / 30 * 8))
    # 5) quartos (até 4)
    if (it.get("rooms") or 0) >= 3:
        pts["quartos"] = 4
    # 6) vaga (até 5)
    if (it.get("parking") or 0) >= 1:
        pts["vaga"] = 5
    # 7) gás (até 7)
    if it.get("gas") is True:
        pts["gas"] = 7
    elif it.get("gas") is False:
        pts["gas"] = -2
    # 8) condomínio barato (até 6)
    if it.get("condo"):
        pts["cond"] = max(-3.0, min(6.0, (700 - it["condo"]) / 500 * 6))
    if it.get("semi_furnished"):
        pts["semi"] = -2
    total = round(max(0.0, min(100.0, sum(pts.values()))), 1)
    return total, {k: round(v, 1) for k, v in pts.items()}


def exception_ok(it, cfg, bairros, median_ppm2):
    """Acima do teto normal só passa se for MUITO boa."""
    if it["price"] <= cfg["preco"]["maximo"]:
        return True
    v = ppm2(it)
    if not v:
        return False
    tier = bairros.tier.get(it["bairro"], 3)
    return tier <= 2 and v <= median_ppm2 * (1 - cfg["preco"]["excecao_desconto_minimo"])


# ----------------------------------------------------------------------------
# Identidade / dedupe
# ----------------------------------------------------------------------------

def listing_id(it):
    return hashlib.sha1(it["url"].encode()).hexdigest()[:12]


def cluster_key(it):
    """Mesmo imóvel anunciado em vários sites: bairro + área + preço aproximados."""
    a = round((it.get("area") or 0) / 2)
    p = round((it.get("price") or 0) / 5000)
    return f"{it['bairro']}|{a}|{p}|{it.get('rooms')}"


# ----------------------------------------------------------------------------
# Armazenamento
# ----------------------------------------------------------------------------

class Store:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"listings": {}, "runs": []}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    @property
    def listings(self) -> dict:
        return self.data["listings"]

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
