# Демо-инструкция и питч (3 минуты)

## Перед выходом на сцену (1 минута)

```powershell
cd C:\Users\nicem\Documents\github\ai_tests\mistral-hack\civicsite
.venv\Scripts\civicsite.exe serve --backend elastic     # http://127.0.0.1:8000
```
Открой в браузере: `http://127.0.0.1:8000` (вкладка 1), Kibana (вкладка 2: Agents / Dev Tools), терминал (вкладка 3).
Если пропал интернет: `civicsite serve --backend local` (то же самое, без семантики).

Что уже готово и проиндексировано: 269 722 записи (6 живых датасетов DOB, Манхэттен), 16 476 флагов, 31 248 зданий. Агент в Kibana Agent Builder: `civicsite-copilot`.

## Сценарий демо

| Время | Что делаешь | Что видно / что говорить |
|---|---|---|
| 0:00 | Слайда нет. Страница `http://127.0.0.1:8000` | "NYC data is fragmented: six DOB datasets, no shared key. CivicSite makes it one cited action queue." |
| 0:20 | Нажми чип **439 East 77 Street** | Карточка здания (BIN/BBL), **Action queue**: флаги с severity, кто владеет задержкой (applicant/agency), next step, ссылка на исходную строку. Клик по ссылке открывает строку в NYC Open Data |
| 0:50 | В карточке флага нажми **Mistral facts** | Mistral structured output: trades, hazard, required actions, key_quotes. Подчеркни: "quotes verified verbatim 100%" |
| 1:15 | Блок **Portfolio**, выбери `OBJECTIONS_AWAITING_RESPONSE`, клик по первой строке | Агрегация Elastic по 31K зданий: самые "застрявшие" заявки (напр. 240 Greenwich St, 33 objections) |
| 1:40 | Терминал: `civicsite search "landlord converted apartments illegally without approval" --record-type violation` | `mode: hybrid_rrf(bm25+mistral_semantic)`: находит violations по смыслу, слов "illegally converted" в запросе может не быть в тексте |
| 2:05 | Блок **Ask**: "What is blocking 439 East 77 Street and who has to act?" | Mistral вызывает `resolve_property → run_preflight_checks → search_project_records`; внизу "citations: N/N verified" |
| 2:30 | Kibana → Agents → `CivicSite Permit Copilot`, задай "What is blocking BIN 1045989?" | Тот же результат через Agent Builder: ES\|QL-инструменты. Покажи MCP URL (в выводе `civicsite agent-builder`) |
| 2:50 | Терминал: `civicsite eval --n 100` (результат уже есть в `eval/report.json`) | "100 flags sampled, 136 cited rows re-read live from Socrata: 100% exist, 100% BIN match, 100% status match" |

## Питч (читать вслух, ~60 секунд)

> Permit delay in New York is mostly **applicant-controlled time**: the Comptroller found plans sit with applicants about 70% of the time. But a small architecture or GC team has to chase that across six DOB datasets with different keys, date formats and a frozen legacy system.
>
> **CivicSite** resolves an address, BIN or BBL across filings, permits, violations and complaints, and returns a ranked action queue: what is blocking the job, **who owns the delay, the applicant or DOB**, what to do next, and the exact source row for every claim.
>
> Elasticsearch is the backbone: explicit mappings, `semantic_text` on a **Mistral embedding endpoint registered inside Elastic**, hybrid BM25-plus-semantic retrieval with RRF, aggregations for portfolio risk, and an **Agent Builder** agent with ES|QL tools exposed over MCP. Mistral does what models are good at: **function calling** to choose the right Elastic tool, and **structured output** that turns messy violation text into typed facts, with every quote verified verbatim.
>
> We do not let the model decide what is wrong. Deterministic rules do that, and every answer's citations are checked against what the tools returned. On 100 sampled flags, every cited record re-fetched live from NYC Open Data matched on existence, building and status.
>
> We also audited the data: legacy filings are frozen in 2020, one permit dataset silently drops 70% of rows because of mixed-case borough names, and 80% of naive "missing permit" flags are amendments that never carry a permit. CivicSite handles all of that. Next: correction-letter OCR with Mistral, then a pilot with NYC expediters.

## Что ожидать и что делать, если что-то пошло не так

- **Страница не открывается:** сервер не запущен, команда `serve` выше. Порт занят: `--port 8001`.
- **Ask / Mistral facts вернули ошибку 503:** нет `MISTRAL_API_KEY` в `.env`. 502/429: лимит Mistral, повтори через несколько секунд.
- **Ask отвечает слабее, чем хочется:** в `.env` стоит `MISTRAL_CHAT_MODEL=mistral-small-2603`. Для агента лучше `mistral-large-4` (есть в API); поменяй и перезапусти `serve`. Это единственное, что влияет на качество ответа, правила от модели не зависят.
- **Поиск показывает `lexical`, а не `hybrid_rrf`:** семантика есть только у 1000 записей (`SEMANTIC_MAX_DOCS=1000`), при вопросах про смысл показывай violations (`--record-type violation`) и открытые заявки. Увеличить лимит: `SEMANTIC_MAX_DOCS=20000`, затем `civicsite build && civicsite index` (~4 мин + эмбеддинги).
- **Честные ограничения (скажи сам, если спросят):** только Манхэттен и последние 12 мес. заявок; флаги это кандидаты на follow-up, не юридический вердикт; precision требует ручной разметки (`eval/labels_template.csv` → `civicsite eval --labels eval/labels.csv`), автоматически подтверждена только точность цитирования.

## Полезные команды

```powershell
civicsite check "1 Wall Street"                   # CLI-отчёт по зданию
civicsite ask "Which buildings have the most unanswered objections?"
civicsite status                                  # счётчики индексов
civicsite agent-builder                           # перерегистрировать агента/инструменты
civicsite eval --n 100                            # метрика цитирования
```
