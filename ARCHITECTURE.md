# Arquitetura do Sistema — Bulletin AI Pipeline

> Documento de referência técnica para a equipe.
> Descreve como o PHP (monolito) e o Python (microserviço de IA) trabalham juntos,
> onde os dados vivem, e como os agentes estão organizados.

---

## 1. Visão Geral

A solução é composta por **dois sistemas distintos** que se comunicam via HTTP:

```
┌─────────────────────────────────────────────────────────┐
│                  PHP MONOLITH (platform)                  │
│                                                           │
│  • CMS multi-tenant (sites de paróquias)                 │
│  • Upload e gestão de PDFs de boletins                   │
│  • OCR e extração de itens (OpenAI / gpt-4o-mini)        │
│  • taskmanager — controle de jobs assíncronos             │
│  • Dashboard de visualização e edição manual             │
│  • Gestão de clientes, projetos, usuários                │
│                                                           │
│  Quando precisa de geração de conteúdo com IA:           │
│       POST /api/...  →  Python Microservice              │
└───────────────────────────┬─────────────────────────────┘
                            │ HTTP (cURL)
                            ▼
┌─────────────────────────────────────────────────────────┐
│              PYTHON MICROSERVICE (worker de IA)           │
│                                                           │
│  • Geração de imagens para redes sociais (Gemini)        │
│  • Geração de HTML trilingue para CMS (Claude)           │
│  • Revisão de qualidade — imagem + HTML (Claude Vision)  │
│  • Sistema de feedback e aprendizado contínuo por client │
│                                                           │
│  Stateless — recebe job, processa com IA, retorna result │
└─────────────────────────────────────────────────────────┘
```

**Princípio central:** o PHP continua sendo o sistema de negócio. O Python é o worker especializado em IA — toda vez que o monolito precisar de geração de conteúdo inteligente, ele delega ao Python via endpoint e aguarda o resultado.

---

## 2. O Lado PHP — O que já existe

### 2.1 Estrutura de pastas por cliente

Cada cliente (paróquia) tem seu próprio diretório no servidor, seguindo o padrão:

```
platform/ctm/{customer_number}/projects/{project_folder}/
    bulletins/
        YYYY/
            YYYYMMDD.pdf                  ← PDF original do boletim
            scan/
                MMDD/
                    pages/                ← PNGs de cada página (gerados pelo PHP)
                    json/
                        page_000_ocr.json ← OCR por página
                        master_ocr.json   ← OCR consolidado
                        final_items.json  ← anúncios extraídos ← ENTRADA do Python
    json/
        project_profile.json              ← perfil e regras do projeto
    feedback/
        image_feedback.md                 ← aprendizado acumulado (imagens)
        html_feedback.md                  ← aprendizado acumulado (HTML)
    support/
        logo.png                          ← logo da paróquia
        placeholder.png                   ← imagem fallback
```

> O `{customer_number}` e o `{project_folder}` vêm do banco de dados (`project` e `customer`).
> O `data_cnx.php` os disponibiliza globalmente via `$pg_variables`.

### 2.2 Arquivos-chave do PHP

| Arquivo | Responsabilidade |
|---|---|
| `library/cm/openAI/bulletin_scan_start.php` | Inicia o scan do boletim; cria registro no `taskmanager` e dispara o worker |
| `library/cm/openAI/bulletin_scan_worker.php` | Worker assíncrono: OCR página por página → classificação → merge → `final_items.json` |
| `library/cm/openAI/api/openai_bootstrap.php` | **Biblioteca central de IA do PHP**: chamada OpenAI, `update_task_status()`, `create_folders()`, `log_ai_tokens()`, `load_profile()` |
| `CAF227A79146/tasks/projects/check_bulletin_upload.php` | Verifica se todas as igrejas ativas enviaram o PDF da semana; envia e-mail de alerta |
| `library/cm/openAI/prompts/*.php` | Prompts versionados por tarefa |
| `library/cm/openAI/schemas/*.json` | Schemas de saída estruturada (JSON Schema para OpenAI) |

### 2.3 Banco de dados relevante

| Tabela | Uso |
|---|---|
| `taskmanager` | Controle de jobs assíncronos (status, resultado, PID, timestamps) |
| `openai` | Log de todos os tokens consumidos por projeto/cliente |
| `project` | Configuração de cada projeto: `project_folder`, `project_type`, `project_active`... |
| `customer` | Dados do cliente: `customer_number` |

---

## 3. O Lado Python — O microserviço de IA

### 3.1 Responsabilidade

O Python **não gerencia clientes, não faz scraping, não faz OCR**. Ele recebe do PHP um `final_items.json` já pronto e produz dois artefatos por anúncio:

- Uma **imagem** (1080×1350px) para redes sociais
- Um **HTML trilingue** (EN / ES / PT-BR) para o CMS

Depois **revisa** a qualidade de ambos e acumula **feedback** para melhorar os próximos runs.

### 3.2 Stack de tecnologias

| Camada | Tecnologia | Para quê |
|---|---|---|
| API do microserviço | `FastAPI` + `uvicorn` | Expõe os endpoints que o PHP consome |
| LLM — geração HTML e revisão | `anthropic` SDK — `claude-opus-4-5` | Agentes 4B e 5 |
| LLM — localização de blocos | `anthropic` SDK — `claude-opus-4-5` | Agente 4A (etapa A) |
| Geração de imagens | Google Gemini `gemini-flash-image` | Agente 4A (etapa C) |
| Manipulação de imagens | `Pillow` (PIL) | Crop, resize, composição de logo |
| Async / paralelismo | `asyncio` nativo Python | Agentes 4A + 4B em paralelo |
| Variáveis de ambiente | `python-dotenv` | Carregamento do `.env` |

### 3.3 Variáveis de ambiente necessárias (`.env`)

```bash
ANTHROPIC_API_KEY=sk-ant-...   # Claude API — Agentes 4A (localização), 4B e 5
GOOGLE_API_KEY=AIza...         # Gemini API — Agente 4A (geração de imagens)
```

---

## 4. Os Agentes Python

Cada agente é um arquivo Python com uma única responsabilidade. Eles não se chamam diretamente — o orquestrador (FastAPI) os coordena.

### Agente 4A — Gerador de Imagens (`agent4a_image.py`)

**Task:** Para cada anúncio em `final_items.json`, gerar um flyer profissional 1080×1350px.

**Rules:**
- Localizar o anúncio na página PNG via bounding box (Claude Vision)
- Nunca cruzar espaços em branco entre blocos distintos na página
- Excluir header e footer da página do bounding box
- Recortar a região isolada com padding de 12px
- Enviar o crop + prompt ao Gemini para redesenho
- Aplicar feedback acumulado de `feedback/image_feedback.md` no prompt
- Sobrepor logo da paróquia no rodapé da imagem final
- Fallback: se Gemini falhar → usar `support/placeholder.png`
- Cache de bounding boxes em `locations.json` (evita rechamada ao Claude em regenerações)

**Inputs:** `pages/page_0NN.png`, `final_items.json`
**Outputs:** `images/announcement_NN.png`, `locations.json`
**APIs:** Claude (localização) + Gemini (geração)

---

### Agente 4B — Gerador de HTML (`agent4b_html.py`)

**Task:** Para cada anúncio, gerar HTML trilingue (EN → ES → PT-BR) pronto para inserção no CMS.

**Rules:**
- Seguir o template HTML da paróquia (`html_template.txt`)
- Estrutura obrigatória por idioma: `<h2>` com data → `<p>/<h4>/<ul>` com conteúdo → `<blockquote>` com contato
- Separar idiomas com `<p align="center" style="color:gray;">- - -</p>`
- Links internos do domínio da paróquia: sem `target="_blank"`
- Links externos: com `target="_blank"`
- Nunca mencionar "QR code" como texto — inserir só o link
- Aplicar feedback acumulado de `feedback/html_feedback.md`
- Detectar QR codes nas páginas PNG e incluir as URLs no HTML

**Inputs:** `final_items.json`, `html_template.txt`, `feedback/html_feedback.md`
**Outputs:** `html/announcement_NN.html`
**APIs:** Claude

---

### Agente 5 — Revisor (`agent5_reviewer.py`)

**Task:** Revisar a qualidade de cada imagem e HTML gerados antes de liberar para o CMS.

**Rules:**
- Verificar imagem: contaminação cruzada, estrutura básica (foto, título, data, logo, local, contato), erros ortográficos, presença de QR code
- Verificar HTML: estrutura nos 3 idiomas, erros ortográficos não marcados em vermelho, menção textual a "QR", HTML bem formado
- Verificar programaticamente se a imagem tem exatamente 1080×1350px
- Consolidar erros que ocorrem nos 3 idiomas em uma única entrada prefixada com `EN/ES/PT:`
- `AUTO_REGEN = False` — revisor reporta; não regenera automaticamente
- Salvar resultado por anúncio em `review_report.json`
- Salvar lista de IDs aprovados em `approved.json`

**Inputs:** `images/`, `html/`, `pages/`, `final_items.json`, `locations.json`
**Outputs:** `review_report.json`, `approved.json`
**APIs:** Claude Vision

---

### Orquestrador — Dashboard / API (`src/dashboard/app.py`)

**Task:** Expor endpoints HTTP para o PHP consumir e coordenar a execução dos agentes.

**Rules:**
- Toda execução é assíncrona: retorna `job_id` imediatamente
- PHP faz polling via `GET /api/workflow/status/{job_id}`
- Agentes 4A e 4B sempre rodam em paralelo (`asyncio.gather`)
- Agente 5 só roda após ambos concluírem

**Endpoints principais expostos ao PHP:**

| Método | Rota | O que faz |
|---|---|---|
| `POST` | `/api/workflow/start` | Inicia pipeline completo para um cliente/data |
| `GET` | `/api/workflow/status/{job_id}` | Polling de status |
| `GET` | `/api/run/{parish}/{date}` | Retorna todos os anúncios com imagem, HTML e review |
| `GET` | `/api/image/{parish}/{date}/{id}` | Serve a imagem PNG |
| `POST` | `/api/regen/image/{parish}/{date}/{id}` | Regenera imagem de um anúncio |
| `POST` | `/api/regen/content/{parish}/{date}/{id}` | Regenera HTML de um anúncio |
| `POST` | `/api/edit/html/{parish}/{date}/{id}` | Salva edição manual do HTML |
| `POST` | `/api/review/{parish}/{date}/{id}` | Aciona revisão individual |
| `POST` | `/api/rate/{parish}/{date}/{id}` | Salva avaliação 1–5 estrelas |
| `POST` | `/api/finalize/{parish}/{date}` | Consolida feedback via IA para o próximo run |
| `GET` | `/api/parishes` | Lista clientes configurados |

---

## 5. Sistema de Feedback e Aprendizado Contínuo

O microserviço Python tem memória entre execuções. Os feedbacks ficam salvos **por cliente** no mesmo diretório de pastas do PHP:

```
ctm/{customer_number}/projects/{project_folder}/feedback/
    image_feedback.md    ← instruções acumuladas para o Agente 4A (Gemini)
    html_feedback.md     ← instruções acumuladas para o Agente 4B (Claude)
```

### Como o aprendizado acontece

```
Run N
  → Usuário avalia imagens e HTMLs (1–5 ⭐) no dashboard PHP
  → Pode escrever instruções específicas por anúncio
  → Clica "Finalizar run"
        → Python consolida ratings + problemas via Claude
        → Gera 3–6 instruções específicas e objetivas
        → Salva em image_feedback.md / html_feedback.md

Run N+1
  → Agente 4A lê image_feedback.md e injeta no prompt do Gemini
  → Agente 4B lê html_feedback.md e injeta no system prompt do Claude
  → Resultados melhoram automaticamente sem alterar código
```

O caminho dos arquivos de feedback é determinado pelo `{customer_number}` e `{project_folder}` vindos do banco de dados — **os mesmos parâmetros que o PHP já usa para tudo**.

---

## 6. Fluxo Completo de Dados

```
[Banco de dados]
    project.customer_number
    project.project_folder
    project.project_type (Church1/Church2/Church3)
           │
           ▼
[PHP — bulletin_scan_worker.php]
    ctm/{customer}/projects/{folder}/bulletins/YYYY/YYYYMMDD.pdf
    → converte páginas em PNG → pages/
    → OCR via OpenAI (gpt-4o-mini)
    → classificação + merge de itens
    → salva final_items.json
           │
           │ POST /api/workflow/start
           │ { parish_id, date, items_path }
           ▼
[Python — Agente 4A + 4B em paralelo]
    lê final_items.json
    lê feedback/image_feedback.md
    lê feedback/html_feedback.md
    lê support/logo.png
    ↓                    ↓
  Gemini             Claude
  (imagens)          (HTML trilingue)
    ↓                    ↓
  images/            html/
           │
           ▼
[Python — Agente 5]
    revisa imagens + HTMLs via Claude Vision
    salva review_report.json + approved.json
           │
           │ resposta ao PHP
           ▼
[PHP — Dashboard CMS]
    exibe cards: imagem gerada + HTML renderizado
    usuário avalia (⭐), edita, regenera
           │
           ▼
[Python — /api/finalize]
    consolida feedback → salva em feedback/*.md
    pronto para o Run N+1
```

---

## 7. Estrutura de Pastas do Microserviço Python

```
project-zero-claude/
│
├── src/
│   ├── agents/
│   │   ├── agent4a_image.py      ← Gerador de imagens (Gemini)
│   │   ├── agent4b_html.py       ← Gerador de HTML trilingue (Claude)
│   │   └── agent5_reviewer.py    ← Revisor de qualidade (Claude Vision)
│   ├── dashboard/
│   │   └── app.py                ← FastAPI: orquestrador + endpoints
│   └── prompts/
│       ├── html_system_prompt.txt
│       └── system_prompt.txt
│
├── .env                          ← ANTHROPIC_API_KEY, GOOGLE_API_KEY
├── .env.example
├── requirements.txt
└── ARCHITECTURE.md               ← este documento
```

> Os dados de cada cliente (PDFs, páginas PNG, JSONs, feedbacks) **não ficam neste repositório**.
> Ficam no servidor, no diretório do PHP: `ctm/{customer_number}/projects/{project_folder}/`.
> O Python acessa esses caminhos via parâmetros recebidos nos endpoints.

---

## 8. Próximos Passos — Direção da Arquitetura

A evolução planejada do microserviço Python é organizar os agentes com **tasks e rules explícitas e configuráveis por cliente**, vindas do banco de dados em vez de arquivos estáticos.

Cada cliente terá no banco:
- Quais páginas do boletim processar (ex: páginas 7–10 ou 1–3)
- Idiomas de saída do HTML
- Estilo visual das imagens
- Regras de conteúdo específicas da paróquia

Isso elimina a constante `BULLETIN_PAGES = [7, 8, 9, 10]` hardcoded no código e torna o microserviço verdadeiramente multi-tenant — o mesmo worker serve qualquer cliente com comportamento configurado dinamicamente.
