# Демо-инструкция и питч (3 минуты)

## 0. Подготовка за 2 минуты до выхода

**Терминал (PowerShell)**, запусти сервер и оставь окно открытым:

```powershell
cd C:\Users\nicem\Documents\github\ai_tests\mistral-hack\civicsite
.venv\Scripts\civicsite.exe serve --backend elastic
```
Ждёшь строку `Uvicorn running on http://127.0.0.1:8000`.
Нет интернета на площадке: `--backend local` вместо `--backend elastic` (всё то же, но без семантики Mistral).

**Открой 4 вкладки браузера в этом порядке:**

| # | Вкладка | Ссылка | Зачем |
|---|---|---|---|
| 1 | CivicSite (главная демка) | http://127.0.0.1:8000 | Основной флоу |
| 2 | Kibana Dev Tools | https://my-elasticsearch-project-e11a9f.kb.us-east4.gcp.elastic.cloud/app/dev_tools#/console | Показать "Elastic-часть": mapping, hybrid-запрос, ES\|QL, inference-эндпоинты |
| 3 | Kibana Agent Builder | https://my-elasticsearch-project-e11a9f.kb.us-east4.gcp.elastic.cloud/app/agent_builder | Агент `CivicSite Permit Copilot` и его ES\|QL-инструменты. Если ссылка не открылась: меню слева (≡) → **Agents** |
| 4 | Источник данных (для клика по цитате) | https://data.cityofnewyork.us/resource/rbx6-tga4.json?tracking_number=526540356 | Подтверждение, что строка реально существует. Обычно открывается кликом из вкладки 1 |

Если Kibana просит логин: тот же аккаунт Elastic Cloud, проект `my-elasticsearch-project-e11a9f`.

Что уже загружено: 269 722 записи (6 живых датасетов DOB, Манхэттен), 25 853 флага, 31 248 зданий.

---

## 1. Сценарий демо (по минутам)

### 0:00 Вкладка 1, http://127.0.0.1:8000
Скажи: "NYC permit data lives in six DOB datasets with no shared key. CivicSite turns it into one cited action queue."
Покажи шапку: `backend: elastic`, `LLM: mistral-small-2603`, портфель ниже ("14 494 buildings with open flags").

### 0:20 Вкладка 1: проверка здания
1. **Клик по чипу `439 East 77 Street`** под полем поиска (или впиши адрес и нажми **Check building**).
2. Что появится: карточка (BIN 1045989, BBL 1014720017), **Action queue** с двумя пунктами:
   - MEDIUM `PERMIT_EXPIRING`: "Permit M00943589-S5-EW-SP expires in 3 days", owner = applicant, next step, ссылка-цитата.
   - LOW `PERIODIC_FILING_OVERDUE`: 2 просроченных отчёта по LL84.
3. Скажи: "Rules decide, not the LLM. Every item cites its source row, controlled by applicant or agency."
4. **Клик по ссылке `permits:526540356`** → откроется строка на NYC Open Data (вкладка 4). Скажи: "Re-fetchable proof".

### 0:50 Вкладка 1: Mistral structured output
В пункте LOW у ссылки `safety_violations:VIO-FTF-EN-BENCH-202605-0161919` нажми кнопку **Mistral facts**. Через пару секунд ниже появится JSON (work_summary, required_actions, key_quotes) и строка **"quotes verified verbatim: 100%"**.
Скажи: "Mistral extracts; we verify its quotes are literally in the record."

### 1:15 Вкладка 1: портфельный вид (агрегации Elastic)
Внизу, в блоке **Portfolio**: выпадающий список → **OBJECTIONS_AWAITING_RESPONSE**. Клик по первой строке (240 Greenwich Street) откроет его карточку.
Скажи: "Aggregations across 31K buildings: where applicant-owned delay is concentrated."

### 1:40 Вкладка 2: Kibana Dev Tools (Elastic-часть)
Вставь запрос и нажми ▶ (по одному):

```
GET _inference/_all
```
(покажи эндпоинты `civicsite-mistral-embeddings` и `civicsite-mistral-chat`: Mistral зарегистрирован внутри Elastic)

```
GET civicsite_records/_mapping
```
(найди поле `description_semantic`: тип `semantic_text`, inference_id = `civicsite-mistral-embeddings`)

```
GET civicsite_records/_search
{
  "size": 3,
  "_source": ["record_id", "address", "status", "description"],
  "retriever": {
    "rrf": {
      "retrievers": [
        { "standard": { "query": { "bool": { "must": [{ "match": { "description": "illegally converted apartments" } }], "filter": [{ "term": { "record_type": "violation" } }] } } } },
        { "standard": { "query": { "bool": { "must": [{ "semantic": { "field": "description_semantic", "query": "landlord split an apartment without approval" } }], "filter": [{ "term": { "record_type": "violation" } }] } } } }
      ],
      "rank_window_size": 50,
      "rank_constant": 20
    }
  }
}
```
(гибрид BM25 + Mistral semantic через RRF)

```
POST _query?format=txt
{ "query": "FROM civicsite_flags | STATS flags = COUNT(*) BY reason_code, severity | SORT flags DESC | LIMIT 10" }
```
(ES|QL: сколько флагов каждого типа)

### 2:10 Вкладка 1: агент Mistral
В блоке **Ask CivicSite** (после проверки здания поле уже заполнено "What is blocking 439 EAST 77 STREET?") нажми **Ask**.
Появится ответ, строка трассы `→ run_preflight_checks(...)` и зелёная строка **"citations: N/N verified against tool results"**.
Скажи: "Mistral function calling picks the Elastic tools; we verify every citation."

### 2:35 Вкладка 3: Agent Builder
Открой агента **CivicSite Permit Copilot** → в чате введи: `What is blocking BIN 1045989?` → в ответе видно вызов инструментов `civicsite.property_flags`, `civicsite.property_timeline` (ES|QL-инструменты).
Скажи: "Same product as an Agent Builder agent, also exposed over MCP."
MCP URL: https://my-elasticsearch-project-e11a9f.kb.us-east4.gcp.elastic.cloud/api/agent_builder/mcp

### 2:50 Терминал (открой второе окно PowerShell)
```powershell
cd C:\Users\nicem\Documents\github\ai_tests\mistral-hack\civicsite
type eval\report.json
```
Скажи: "100 sampled flags, 136 cited rows re-read live from NYC Open Data: 100% exist, same building, same status."

---

## 2. Питч (читать вслух, ~60 секунд)

> Permit delay in New York is mostly **applicant-controlled time**: the Comptroller found plans sit with applicants about 70% of the time. But a small architecture or GC team has to chase that across six DOB datasets with different keys, date formats and a frozen legacy system.
>
> **CivicSite** resolves an address, BIN or BBL across filings, permits, violations and complaints, and returns a ranked action queue: what is blocking the job, **who owns the delay, the applicant or DOB**, what to do next, and the exact source row for every claim.
>
> Elasticsearch is the backbone: explicit mappings, `semantic_text` on a **Mistral embedding endpoint registered inside Elastic**, hybrid BM25-plus-semantic retrieval with RRF, aggregations for portfolio risk, and an **Agent Builder** agent with ES|QL tools exposed over MCP. Mistral does what models are good at: **function calling** to choose the right Elastic tool, and **structured output** that turns messy violation text into typed facts, with every quote verified verbatim.
>
> We do not let the model decide what is wrong. Deterministic rules do that, and every answer's citations are checked against what the tools returned. On 100 sampled flags, every cited record re-fetched live from NYC Open Data matched on existence, building and status.
>
> We also audited the data: legacy filings are frozen in 2020, one permit dataset silently drops 70% of rows because of mixed-case borough names, and most naive "missing permit" flags are amendments that never carry a permit. CivicSite handles all of that. Next: correction-letter OCR with Mistral, then a pilot with NYC expediters.

---

## 3. Если что-то пошло не так

| Симптом | Что делать |
|---|---|
| Вкладка 1 не открывается | Сервер не запущен: команда из пункта 0. Порт занят: добавь `--port 8001` и открой `http://127.0.0.1:8001` |
| Ask / Mistral facts дают ошибку | Проверь `MISTRAL_API_KEY` в `.env`; при 429 подожди несколько секунд |
| Ask отвечает слабо | В `.env` поменяй `MISTRAL_CHAT_MODEL=mistral-large-4` и перезапусти `serve` (правила от модели не зависят) |
| Hybrid-запрос не находит по смыслу | Семантика есть у 1000 записей (`SEMANTIC_MAX_DOCS=1000`): показывай на `record_type: violation` и открытых заявках. Больше: `SEMANTIC_MAX_DOCS=20000`, затем `civicsite build` и `civicsite index` |
| Kibana-ссылка не открылась | Cloud console → проект `my-elasticsearch-project-e11a9f` → **Open Kibana** |

**Честные ограничения (скажи сам, если спросят):** только Манхэттен и последние 12 мес. заявок; флаги это кандидаты на follow-up, не юридический вердикт; precision требует ручной разметки (`eval\labels_template.csv`), автоматически подтверждена только точность цитирования.

## Полезные команды

```powershell
civicsite check "1 Wall Street"            # CLI-отчёт по зданию
civicsite search "unauthorized conversion" --record-type violation
civicsite ask "Which buildings have the most unanswered objections?"
civicsite status                           # счётчики индексов
civicsite agent-builder                    # перерегистрировать агента и инструменты
civicsite eval --n 100                     # метрика цитирования
```
