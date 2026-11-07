# Apto Hunter 🏠

Robô que procura todo dia apartamentos à venda em Porto Alegre que batem com as suas regras,
dá uma **nota de oportunidade** para cada um, manda as novidades no **Telegram** e publica um
**ranking** numa página web (GitHub Pages) com link para o anúncio original.

Fontes: **ZAP, VivaReal, OLX, QuintoAndar, Imovelweb e Chaves na Mão**. As grandes imobiliárias de
POA (Foxter, Auxiliadora Predial, Crédito Real, Guarida…) também anunciam nesses portais, então já
aparecem por eles. O mesmo imóvel anunciado em vários sites vira um card só, com todos os links.

Tudo roda de graça: GitHub Actions (agendamento) + GitHub Pages (página) + Telegram (aviso).

---

## Como funciona

1. Todo dia às ~7h, o GitHub Actions abre um Chrome sem tela, visita as buscas de cada site
   (bairro a bairro, já com filtro de preço/área/quartos) e lê os anúncios.
2. Normaliza tudo, descarta o que não bate (fora da região, mobiliado, leilão, < 60 m², < 2 quartos, caro).
3. Abre a página dos anúncios novos para ler a descrição e detectar **aquecimento a gás** / mobília.
4. Calcula a nota (0–100):

| Critério | Peso |
|---|---|
| R$/m² comparado à mediana da região (o principal sinal de "oferta boa") | até 40 |
| Localização (nível 1: São João, Passo d'Areia/IAPI, Petrópolis, Auxiliadora) | 20 / 13 / 5 |
| Preço absoluto (mais folga no financiamento) | até 10 |
| Área acima de 60 m² | até 8 |
| Aquecimento a gás | +7 (chuveiro elétrico: −2) |
| Condomínio baixo | até 6 |
| Vaga | +5 |
| 3+ quartos | +4 |

5. **Regra dos 280 mil:** entre R$ 250 e 280 mil só entra se o R$/m² estiver pelo menos 15% abaixo da
   mediana da região **e** o bairro for nível 1 ou 2.
6. Salva o histórico em `data/listings.json` (detecta queda de preço), gera `docs/data.json` e
   manda o resumo no Telegram.

Todas as regras ficam em **`config.yaml`** (bairros, pesos de região, preço, páginas por site…).

---

## Configuração (uns 15 minutos, uma vez só)

### 1. Bot do Telegram
1. No Telegram, fale com **@BotFather** → `/newbot` → escolha um nome. Ele te dá um **token**
   (algo como `123456:ABC-...`).
2. Abra a conversa com o seu bot novo e mande qualquer mensagem (ex.: "oi").
3. No navegador, abra `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates` e copie o número em
   `"chat":{"id": ...}` — esse é o **chat id**.

### 2. Repositório no GitHub
1. Crie um repositório **público** chamado `apto-hunter` (o GitHub Pages é grátis em repositório
   público; em privado precisa de plano pago). Os dados são só anúncios públicos; o token do Telegram
   fica em segredo.
2. Na pasta do projeto:
   ```bash
   git init -b main
   git add .
   git commit -m "Apto Hunter"
   git remote add origin https://github.com/SEU-USUARIO/apto-hunter.git
   git push -u origin main
   ```
3. **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_TOKEN` = token do BotFather
   - `TELEGRAM_CHAT_ID` = seu chat id
4. **Settings → Pages** → *Build and deployment*: Source **Deploy from a branch**, Branch **main**,
   pasta **/docs** → Save. A página fica em `https://SEU-USUARIO.github.io/apto-hunter/`.
5. Coloque esse endereço em `site_url` no `config.yaml` (é o link que vai no Telegram) e faça push.
6. **Actions → Busca diária de apartamentos → Run workflow** para rodar a primeira vez agora.
   A primeira execução trata tudo como "novo"; daí em diante só chegam as novidades.

---

## Se algum site bloquear o GitHub

Portais como ZAP/VivaReal/Imovelweb usam Cloudflare e às vezes bloqueiam IPs de datacenter.
O Telegram avisa quando uma fonte voltou vazia ("⚠️ Sem resultados de: …"). Se isso virar
rotina, rode no seu próprio PC (IP residencial quase nunca é bloqueado):

```powershell
.\run_local.ps1
```

O script cria o ambiente, roda a busca e faz o push — a página e o Telegram funcionam igual.
Dá para agendar no **Agendador de Tarefas do Windows** (instruções no topo do arquivo) e desativar o
agendamento do Actions (comentando o bloco `schedule` em `.github/workflows/daily.yml`).

## Rodar manualmente / testar

```bash
pip install -r requirements.txt
python -m playwright install chromium
python -m apto.main --no-telegram                 # roda tudo sem mandar mensagem
python -m apto.main --only olx quintoandar --headed   # só algumas fontes, vendo o navegador
python -m apto.main --raw-file tests/fixture_raw.json --no-telegram  # teste offline
```

Para ver a página localmente: `python -m http.server -d docs` e abra http://localhost:8000.

## Estrutura

```
config.yaml            regras da busca (edite aqui)
apto/sources.py        coleta em cada site (Playwright)
apto/core.py           leitura dos anúncios, filtros, nota, dedupe
apto/notify.py         mensagem do Telegram
apto/main.py           orquestra tudo
docs/index.html        página do ranking (GitHub Pages)
data/listings.json     histórico (gerado)
.github/workflows/     agendamento diário
run_local.ps1          alternativa: rodar no seu PC
```

## Limitações conhecidas
- Sites mudam o HTML de vez em quando; se uma fonte zerar por vários dias, o extrator dela
  (`JS_*` em `apto/sources.py`) precisa de ajuste.
- "Gás" é detectado por texto da descrição; quando o anúncio não fala nada fica como desconhecido
  (sem bônus nem penalidade).
- A mediana de R$/m² é calculada com os próprios anúncios coletados nos últimos 30 dias (até haver
  8 amostras usa R$ 4.200/m² como referência).
- Use com moderação: o robô faz poucas requisições por dia, com pausas, só para uso pessoal.
