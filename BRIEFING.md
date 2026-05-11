# Briefing Técnico: Pipeline Automatizado de Processamento de Boletins Paroquiais

*Última atualização: 2026-05-08 — versão 2.0 (sistema em produção)*

---

## 1. Visão Geral e Objetivos

### Contexto

Pipeline automatizado que processa boletins semanais de igrejas católicas nos EUA. O sistema realiza o fluxo completo desde o acesso ao website da paróquia até a geração de conteúdo formatado (imagem para redes sociais + HTML para CMS) com revisão de qualidade por IA.

**Paróquia atual em produção:** St. Katharine Drexel Catholic Church (`skdrexel`), Weston FL.

### Objetivos

- Eliminar trabalho manual de leitura, triagem e formatação de anúncios
- Gerar flyers sociais (1080×1350px) consistentes com o estilo visual da paróquia
- Produzir HTML trilingue (EN/ES/PT-BR) limpo e pronto para inserção no CMS
- Garantir rastreabilidade, controle de qualidade e aprendizado contínuo via feedback

---

## 2. Arquitetura Real do Sistema

O sistema é composto por **5 agentes** + **1 dashboard FastAPI** que funciona tanto como interface de visualização quanto como orquestrador do pipeline.

> **Nota:** O "Agente 3" (Orquestrador separado) do design original foi absorvido pelo dashboard (`app.py`), que coordena a execução de todos os agentes via `asyncio`.

```
┌──────────────────────────────────────────────────────────────────────┐
│                      PIPELINE (acionado pelo Dashboard)               │
│                                                                        │
│  [Dashboard / FastAPI]  ──►  Agente 1 (Scraper)                       │
│         │                          │ PDF                               │
│         │                          ▼                                   │
│         │               Agente 2 (Reader/Claude)                       │
│         │                          │ announcements.json                │
│         │                     ┌────┴─────┐  (paralelo asyncio)         │
│         │                     ▼          ▼                             │
│         │          Agente 4A (Imagens) Agente 4B (HTML)                │
│         │          [Claude+Gemini]    [Claude API]                     │
│         │                     └────┬─────┘                             │
│         │                          │                                   │
│         │               Agente 5 (Reviewer/Claude)                     │
│         │                          │ review_report.json                │
│         └──────────────────────────┘                                   │
│                         Dashboard (visualização + edição manual)       │
└──────────────────────────────────────────────────────────────────────┘
```

### Resumo dos Agentes

| Arquivo | Função | APIs Utilizadas |
|---|---|---|
| `agent1_scraper.py` | Download do PDF do boletim | httpx, Playwright (fallback) |
| `agent2_reader.py` | Extração dos anúncios do PDF | Claude API (Vision) |
| `agent4a_image.py` | Geração de flyers para redes sociais | Claude API (localização) + Gemini (geração) |
| `agent4b_html.py` | Geração de HTML trilingue para CMS | Claude API |
| `agent5_reviewer.py` | Revisão de qualidade imagem + HTML | Claude API (Vision) |
| `dashboard/app.py` | Orquestração + Interface web | FastAPI, todos os agentes acima |

---

## 3. Fluxo Detalhado de Dados

### Passo 1 — Agente 1: Scraper (`agent1_scraper.py`)

**Entrada:** URL da página de boletins da paróquia (do `config/parishes/skdrexel.yaml`)

**Lógica:**
1. Tenta endpoints conhecidos com `httpx` (sem browser): `bulletin_archives.php?year=YYYY` e variações
2. Extrai links PDF via regex: `href=[\"']([^\"']+\.pdf)[\"']`
3. Ordena por nome de arquivo (padrão `YYYYMMDD.pdf`) e pega o mais recente
4. Fallback: aciona Playwright (Chromium headless) para páginas com JS
5. Download do PDF com headers realistas (User-Agent Chrome)

**Saída:** `output/skdrexel/YYYY-MM-DD/YYYYMMDD.pdf`

**Estrutura de pasta:** usa a data extraída do nome do arquivo (padrão `YYYYMMDD.pdf`), criando automaticamente `output/{parish_id}/{YYYY-MM-DD}/`.

---

### Passo 2 — Agente 2: Reader (`agent2_reader.py`)

**Entrada:** Caminho do PDF + `parish_id`

**Lógica:**
1. Converte **páginas 7, 8, 9 e 10** do PDF em imagens PNG a 150 DPI via `pdf2image`
2. Salva as páginas em `output/{parish}/date/pages/page_07.png` ... `page_10.png`
3. Carrega imagens de exemplo de referência de `config/parishes/{parish_id}/examples/` (few-shot)
4. Monta mensagem multimodal para Claude com:
   - Exemplos de referência (imagens de boletins anteriores como few-shot)
   - Data de hoje (para filtrar eventos passados)
   - As 4 páginas do boletim atual
5. Envia para `claude-opus-4-5` com system prompt especializado
6. Parseia JSON retornado e salva em `announcements.json`

**System prompt:** `src/prompts/system_prompt.txt` — define critérios exatos do que é e não é anúncio.

**Saída JSON por anúncio:**
```json
{
  "id": "1",
  "title": "Santo Rosario en el Jardín de la Virgen",
  "body": "texto completo preservando quebras de linha",
  "category": "events | ministries | formation | fundraising | community | other",
  "event_date": "2026-05-13",
  "location": "Jardín de la Virgen",
  "order": 1,
  "image_prompt": "prompt descritivo para geração de imagem"
}
```

**Saída:** `output/{parish}/date/announcements.json`

---

### Passo 3 — Agente 4A: Gerador de Imagens (`agent4a_image.py`)

> Roda em paralelo com o Agente 4B via `asyncio.gather`.

**Entrada:** `announcements.json` + páginas PNG em `pages/`

**Lógica em 4 etapas:**

**Etapa A — Localização via Claude:**
- Envia as 4 páginas do boletim para `claude-opus-4-5` com lista dos anúncios
- Claude retorna bounding boxes por porcentagem: `{id, page, top, left, bottom, right}`
- Regras explícitas: excluir header/footer da página, nunca cruzar espaços em branco entre anúncios
- Cache salvo em `locations.json` para reutilização em edições individuais

**Etapa B — Crop do anúncio:**
- Recorta a região exata do anúncio da página PNG com padding de 12px
- Resultado: imagem isolada apenas daquele anúncio

**Etapa C — Geração via Gemini:**
- Prompt construído com: título, data, local, emails, telefones, URLs extraídos do corpo
- Aplica feedback acumulado de `config/parishes/{parish_id}/agent_feedback/image_feedback.md`
- Envia o **crop isolado** + prompt para `gemini-3.1-flash-image-preview` (geração de imagem)
- Modelo Gemini redesenha o anúncio como flyer profissional vertical (Instagram-style)
- Fallback: se Gemini falhar → usa `placeholder.png` da pasta `support/`, ou o próprio crop redimensionado

**Etapa D — Composição final:**
- Redimensiona para `1080×1350px` (cover fit)
- Sobrepõe logo da paróquia na parte inferior central (`config/parishes/{parish_id}/logo.png`)
- Salva em `output/{parish}/date/images/announcement_NN.png`

**Saída:** `output/{parish}/date/images/announcement_01.png` ... `announcement_NN.png`

---

### Passo 4 — Agente 4B: Gerador de HTML (`agent4b_html.py`)

> Roda em paralelo com o Agente 4A via `asyncio.gather`.

**Entrada:** `announcements.json` + `parish_id`

**Lógica:**
1. Detecta QR codes nas páginas PNG via `pyzbar` → extrai URLs
2. Carrega system prompt de `src/prompts/html_system_prompt.txt`, injetando:
   - Template HTML da paróquia (`config/parishes/{parish_id}/html_template.txt`)
   - Domínio interno da paróquia (`skdrexel.org`) para links internos sem `target="_blank"`
   - Feedback acumulado de `html_feedback.md`
3. Para cada anúncio, envia um user message com: título, categoria, data, local e corpo
4. Claude gera HTML trilingue (EN → ES → PT-BR) separado por `<p align="center" style="color:gray;">- - -</p>`
5. Estrutura HTML obrigatória em 3 blocos por idioma:
   - **Bloco 1:** `<h2 align="center">` com data/hora formatada
   - **Bloco 2:** `<p>`, `<h4>`, `<ul>` com conteúdo descritivo
   - **Bloco 3:** `<blockquote>` com contato, local, links

**Saída:** `output/{parish}/date/html/announcement_NN.html` (um arquivo por anúncio)

---

### Passo 5 — Agente 5: Revisor (`agent5_reviewer.py`)

**Entrada:** `announcements.json` + `images/` + `html/` + `pages/` (para crop de referência)

**Lógica:**
1. Para cada anúncio, envia ao `claude-opus-4-5`:
   - Imagem fonte (crop original do boletim)
   - Imagem gerada (flyer)
   - HTML gerado
   - Dados do anúncio (título, data, local, categoria)
2. Claude verifica:
   - **Imagem:** contaminação cruzada, estrutura básica (foto, título, data, logo, local, contato), erros ortográficos, QR codes
   - **HTML:** estrutura nos 3 idiomas, erros ortográficos não marcados, termos "QR" como texto, HTML bem formado
   - **Tamanho:** verifica programaticamente se a imagem é 1080×1350px
3. Retorna JSON estruturado: `{image: {approved, issues}, html: {approved, spelling_errors, issues}, overall_approved}`
4. `AUTO_REGEN = False` — revisor apenas reporta, não regenera automaticamente (edição manual pelo dashboard)
5. Salva `review_report.json` e `approved.json`

**Sistema de correção sugerida:**
- Endpoint `/api/suggest-corrections` envia erros ao `CORRECTION_SYSTEM_PROMPT`
- Claude retorna lista de correções exatas: `{original, suggestion, context}` para aplicação cirúrgica no HTML

---

### Passo 6 — Dashboard (`src/dashboard/app.py`)

**Stack:** FastAPI + HTML/JS vanilla + Tailwind CSS (via CDN) | Porta: `localhost:8502`

**Modos de execução do workflow:**
| Modo | O que faz |
|---|---|
| `complete` | Agente 1 → 2 → 4A+4B (paralelo) → 5 |
| `images` | Usa run mais recente → apenas regenera imagens (4A) |
| `content` | Usa run mais recente → apenas regenera HTML (4B) |

**Endpoints principais:**

| Método | Rota | Função |
|---|---|---|
| `GET` | `/api/runs` | Lista todos os runs disponíveis |
| `GET` | `/api/run/{parish}/{date}` | Retorna anúncios com imagem, HTML, review e ratings |
| `GET` | `/api/image/{parish}/{date}/{id}` | Serve imagem PNG como FileResponse |
| `POST` | `/api/workflow/start` | Inicia workflow em background (retorna job_id) |
| `GET` | `/api/workflow/status/{job_id}` | Polling do status do workflow |
| `POST` | `/api/regen/image/{parish}/{date}/{id}` | Regenera imagem de um anúncio específico |
| `POST` | `/api/regen/content/{parish}/{date}/{id}` | Regenera HTML de um anúncio específico |
| `POST` | `/api/edit/html/{parish}/{date}/{id}` | Salva edição manual do HTML |
| `POST` | `/api/review/{parish}/{date}/{id}` | Aciona revisão individual de um anúncio |
| `POST` | `/api/suggest-corrections/{parish}/{date}/{id}` | Sugere correções cirúrgicas de HTML |
| `POST` | `/api/feedback/{parish}/{date}/{id}` | Salva rating + instrução de feedback |
| `POST` | `/api/finalize/{parish}/{date}` | Gera feedback consolidado via IA para próximos runs |
| `POST` | `/api/rate/{parish}/{date}/{id}` | Salva avaliação 1–5 estrelas de imagem e HTML |
| `GET` | `/api/parishes` | Lista paróquias configuradas |
| `GET` | `/api/css/{parish_id}` | Retorna CSS da paróquia para preview do HTML |

**Funcionalidades do frontend:**
- Cards por anúncio: imagem gerada + HTML renderizado lado a lado
- Status visual: aprovado / precisa revisão / pendente (com ícones e cores)
- Botão download da imagem (PNG)
- Botão copiar HTML (clipboard API)
- Editor inline de HTML com salvar
- Regenerar imagem ou HTML individualmente (com instrução pontual opcional)
- Sistema de avaliação 1–5 estrelas por imagem e HTML
- Botão "Finalizar run" → gera feedback IA consolidado e persiste em `agent_feedback/`

---

## 4. Sistema de Feedback e Aprendizado Contínuo

O pipeline tem memória entre execuções via arquivos Markdown em `config/parishes/{parish_id}/agent_feedback/`:

### `image_feedback.md`
Instruções acumuladas para o Agente 4A (Gemini). Geradas de duas formas:
1. **Manual:** usuário escreve instrução ao dar feedback via dashboard
2. **Automático:** ao "Finalizar run", Claude analisa ratings e problemas reportados e gera 3–6 instruções específicas

O agente lê esse arquivo no início de cada run e injeta as instruções no prompt do Gemini.

### `html_feedback.md`
Mesma estrutura, para o Agente 4B (Claude HTML).

### Fluxo de aprendizado:
```
Run N → Usuário avalia (1-5⭐) + aponta problemas
       → "Finalizar run" → Claude consolida aprendizados
       → Salva em image_feedback.md / html_feedback.md
       → Run N+1 usa esses feedbacks automaticamente
```

---

## 5. Estrutura de Arquivos do Projeto

```
Project Zero - Claude/
│
├── config/
│   └── parishes/
│       ├── skdrexel.yaml              ← Config da paróquia (URL, domínio interno)
│       └── skdrexel/
│           ├── html_template.txt      ← Template HTML de referência para o CMS
│           ├── newspage_rules.css     ← CSS do CMS (para preview fiel no dashboard)
│           ├── examples/              ← Screenshots de boletins anteriores (few-shot para Agente 2)
│           ├── examples 2/            ← Pares antes/depois para referência visual
│           ├── support/
│           │   ├── logo-skdrexel.png  ← Logo sobreposto nas imagens geradas
│           │   └── placeholder.png    ← Imagem fallback quando Gemini falha
│           └── agent_feedback/
│               ├── image_feedback.md  ← Feedback acumulado para Agente 4A
│               └── html_feedback.md   ← Feedback acumulado para Agente 4B
│
├── src/
│   ├── agents/
│   │   ├── agent1_scraper.py
│   │   ├── agent2_reader.py
│   │   ├── agent4a_image.py
│   │   ├── agent4b_html.py
│   │   └── agent5_reviewer.py
│   ├── dashboard/
│   │   ├── app.py                     ← FastAPI: orquestrador + interface web
│   │   └── static/
│   │       ├── index.html             ← Frontend completo (HTML/JS vanilla)
│   │       └── *.png                  ← Ícones da interface
│   └── prompts/
│       ├── system_prompt.txt          ← Prompt Agente 2 (extração de anúncios)
│       └── html_system_prompt.txt     ← Prompt Agente 4B (geração HTML trilingue)
│
├── output/
│   └── skdrexel/
│       └── YYYY-MM-DD/
│           ├── YYYYMMDD.pdf           ← PDF original baixado
│           ├── announcements.json     ← Anúncios extraídos pelo Agente 2
│           ├── locations.json         ← Bounding boxes (cache do Agente 4A)
│           ├── review_report.json     ← Resultado da revisão por anúncio
│           ├── approved.json          ← Lista de IDs aprovados
│           ├── ratings.json           ← Avaliações 1-5⭐ por anúncio
│           ├── pages/                 ← PNG das páginas 7–10 do boletim
│           ├── images/                ← Flyers gerados (1080×1350px)
│           └── html/                  ← HTMLs trilingues gerados
│
├── requirements.txt
├── .env                               ← ANTHROPIC_API_KEY, GOOGLE_API_KEY
└── .env.example
```

---

## 6. Configuração de Paróquia (`config/parishes/skdrexel.yaml`)

```yaml
parish:
  id: "skdrexel"
  name: "St. Katharine Drexel Catholic Church"
  bulletin_archive_url: "https://www.skdrexel.org/CatholicChurch.php?pg=Bulletin+Archive"
  internal_domain: "skdrexel.org"

scraper:
  wait_for_js: true
  timeout_seconds: 30
  pdf_link_pattern: "(?i)(bulletin|boletim).*\\.pdf$"

image_generator:
  api: "openai_dalle3"          # legado — Gemini usado na prática
  style_instructions: "Watercolor style, warm and welcoming..."
  size: "1024x1024"
  max_images_per_run: 10
```

---

## 7. Variáveis de Ambiente

```bash
ANTHROPIC_API_KEY=sk-ant-...   # Claude API (Agentes 2, 4B, 5 e localização do 4A)
GOOGLE_API_KEY=...             # Gemini API (geração de imagens no Agente 4A)
```

---

## 8. Stack de Tecnologias em Uso

| Camada | Tecnologia | Uso Real |
|---|---|---|
| **LLM principal** | `anthropic` SDK — `claude-opus-4-5` | Agentes 2, 4B, 5 (e localização no 4A) |
| **Geração de imagem** | Google Gemini `gemini-3.1-flash-image-preview` | Agente 4A |
| **Web scraping** | `httpx` + `playwright` (fallback) | Agente 1 |
| **PDF → imagem** | `pdf2image` (wrapper do poppler) | Agente 2 |
| **Leitura de QR** | `pyzbar` | Agente 4B |
| **Manipulação de imagem** | `Pillow` | Crop, resize, composição de logo |
| **Backend API** | `FastAPI` + `uvicorn` | Dashboard (porta 8502) |
| **Frontend** | HTML/JS vanilla + Tailwind CSS (CDN) | Interface do dashboard |
| **Env vars** | `python-dotenv` | Carregamento de `.env` |
| **Config** | `PyYAML` | Leitura de `skdrexel.yaml` |

---

## 9. Estimativa de Tokens e Custos por Execução

### Agente 2 — Extração de Anúncios (Claude Opus 4.5)

| Item | Detalhe | Tokens estimados |
|---|---|---|
| System prompt | ~400 palavras | ~600 tokens input |
| Imagens de exemplo (few-shot) | ~23 screenshots PNG, ~150KB cada | ~8.000 tokens input (vision) |
| 4 páginas do boletim (PNG 150dpi) | ~200–400KB cada | ~5.000–8.000 tokens input |
| Instrução de data + contexto | ~50 tokens | ~50 tokens input |
| **Total input** | | **~14.000–17.000 tokens** |
| Resposta JSON (9 anúncios) | ~1.500–2.500 palavras | ~2.000–3.000 tokens output |
| **Custo estimado** | claude-opus-4-5: $15/$75 por 1M | **~$0.21–$0.26** |

### Agente 4A — Localização (Claude Opus 4.5)

| Item | Detalhe | Tokens estimados |
|---|---|---|
| 4 páginas PNG + lista de anúncios | Imagens + texto | ~5.500–8.000 tokens input |
| Resposta JSON de bounding boxes | 9 itens | ~300–500 tokens output |
| **Custo estimado** | | **~$0.08–$0.12** |

### Agente 4A — Geração de Imagens (Gemini Flash Image Preview)

| Item | Detalhe | Custo estimado |
|---|---|---|
| Geração por imagem | crop PNG + prompt ~200 tokens | ~$0.005–$0.01/imagem |
| 9 imagens no run | | **~$0.05–$0.09** |

### Agente 4B — Geração HTML (Claude Opus 4.5)

| Item | Detalhe | Tokens estimados |
|---|---|---|
| System prompt (com template) | ~800–1.200 palavras + template | ~1.500 tokens input |
| Dados de cada anúncio | ~100–300 tokens | ~200 tokens input/anúncio |
| HTML trilingue gerado | ~800–1.500 tokens/anúncio | ~1.000 tokens output |
| **Por anúncio** | | **~$0.025–$0.045** |
| **9 anúncios** | | **~$0.22–$0.40** |

### Agente 5 — Revisão (Claude Opus 4.5)

| Item | Detalhe | Tokens estimados |
|---|---|---|
| System prompt de revisão | ~600 tokens | ~600 tokens input |
| Por anúncio: crop fonte + imagem gerada + HTML + dados | Imagens ~3.000–5.000 tokens + HTML ~1.000 | ~5.000–7.000 tokens input |
| Resposta JSON de revisão | ~200–400 tokens | ~300 tokens output |
| **Por anúncio** | | **~$0.08–$0.11** |
| **9 anúncios** | | **~$0.72–$0.99** |

### Resumo Total por Execução Completa (9 anúncios)

| Agente | API | Custo estimado |
|---|---|---|
| Agente 2 (extração) | Claude Opus 4.5 | ~$0.21–$0.26 |
| Agente 4A localização | Claude Opus 4.5 | ~$0.08–$0.12 |
| Agente 4A geração | Gemini Flash Image | ~$0.05–$0.09 |
| Agente 4B HTML | Claude Opus 4.5 | ~$0.22–$0.40 |
| Agente 5 revisão | Claude Opus 4.5 | ~$0.72–$0.99 |
| **Total por run** | | **~$1.28–$1.86** |
| **Mensal (4 runs)** | | **~$5.12–$7.44/paróquia** |

> **Maior custo:** Agente 5 (revisão), pois envia 2 imagens por anúncio ao Claude Vision.
> **Potencial de otimização:** migrar o Agente 2 para `claude-sonnet-4-6` (80% mais barato que Opus) poderia reduzir o custo total em ~15%.

---

## 10. Decisões Arquiteturais Tomadas

| Decisão | Escolha Feita | Justificativa |
|---|---|---|
| **Gerador de imagens** | Gemini `gemini-3.1-flash-image-preview` | Suporte a image-in/image-out; redesenha o crop original preservando elementos visuais |
| **Envio do PDF** | Conversão para PNG (páginas específicas) | Controle exato das páginas; Claude Vision lida melhor com imagens isoladas que PDF completo |
| **Orquestrador** | Absorvido pelo `app.py` (FastAPI) | Elimina um processo separado; workflow controlado via async jobs com polling |
| **Dashboard** | FastAPI + HTML/JS vanilla | Já implementado; sem dependência de build frontend |
| **Feedback persistido** | Arquivos Markdown por paróquia | Simples, legível por humanos, editável manualmente se necessário |
| **AUTO_REGEN** | Desativado (`False`) no Agente 5 | Edição humana via dashboard é mais precisa que regeneração automática cega |
| **Bounding box cacheado** | `locations.json` por run | Evita chamada extra ao Claude a cada regeneração individual de imagem |
| **Idiomas no HTML** | Sempre EN + ES + PT-BR | Paróquia bilíngue (inglês/espanhol) com demanda de PT-BR para comunidade brasileira |

---

## 11. Pontos de Atenção Atuais

### 11.1 Páginas Fixas do Boletim
O Agente 2 processa **fixamente as páginas 7, 8, 9 e 10** (`BULLETIN_PAGES = [7, 8, 9, 10]`). Se o layout do boletim mudar (ex: anúncios em outras páginas), essa constante precisa ser ajustada manualmente.

### 11.2 Modelo Gemini de Imagem
O modelo `gemini-3.1-flash-image-preview` é experimental/preview. Mudanças de API ou descontinuação podem exigir adaptação. O nome do modelo deve ser monitorado nas notas de lançamento do Google.

### 11.3 Erros Ortográficos no HTML
O revisor identifica erros ortográficos mas não corrige automaticamente. O endpoint `/api/suggest-corrections` fornece correções pontuais que precisam ser aplicadas manualmente no editor do dashboard. Fluxo ainda não totalmente automatizado.

### 11.4 Dependência de `requirements.txt` Desatualizado
O `requirements.txt` ainda lista `openai>=1.35.0` e `streamlit>=1.35.0`, mas nenhum dos dois é mais utilizado na implementação atual (Gemini substituiu DALL-E; FastAPI substituiu Streamlit). Limpeza recomendada.

### 11.5 Escalabilidade Multi-Paróquia
O sistema foi projetado para multi-tenant mas só foi validado com `skdrexel`. A segunda paróquia exigirá: novo YAML, novo `html_template.txt`, novas imagens de exemplo e testes dos prompts.

---

## 12. Como Executar

### Dashboard (modo recomendado)
```bash
cd "Project Zero - Claude"
source .venv/bin/activate
python src/dashboard/app.py
# Abre automaticamente http://localhost:8502
```

### Agentes individualmente (modo desenvolvimento/debug)
```bash
# Agente 1 — baixar PDF
python src/agents/agent1_scraper.py

# Agente 2 — extrair anúncios (requer PDF já baixado)
python src/agents/agent2_reader.py

# Agente 4A — gerar imagens (requer announcements.json + pages/)
python src/agents/agent4a_image.py

# Agente 4B — gerar HTML (requer announcements.json)
python src/agents/agent4b_html.py

# Agente 5 — revisar (requer images/ + html/)
python src/agents/agent5_reviewer.py
```

Cada agente tem seu `if __name__ == "__main__"` com `output_dir` e `parish_id` hardcoded para execução local — ajustar conforme necessário.
