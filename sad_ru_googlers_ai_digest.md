# Sad RU Googlers AI Digest Archive

Хранилище ценных находок, скиллов, промптов и архитектурных решений по ИИ и автоматизации разработки из чата `Sad RU Googlers (HISTORY OFF)` (`spaces/AAAAn_HZbYs`), где история очищается каждые 24 часа.

---

## 2026-10-30

- **Источник**: Тред `spaces/AAAAn_HZbYs/threads/O9HuiRSaKgY` (автор топика: Pavel Razumovskiy `prazumovskiy@google.com`, 05:56 ET / 09:56 UTC).
- **Тема**: Автономные многоагентные дебаты, Best-of-N, рой (`swarm`) и ограничение галлюцинаций.

### 1. Протокол дебатов `research-with-debate-council`
- **Автор**: Alexander Milantev (`alexmilan@google.com`, 12:11 и 14:38 ET).
- **Путь в репозитории**: [`/google/src/files/head/depot/configs/users/alexmilan/_agents/skills/research-with-debate-council/SKILL.md`](file:///google/src/files/head/depot/configs/users/alexmilan/_agents/skills/research-with-debate-council/SKILL.md).
- **Ключевые инварианты**:
  1. *Scope Flattening*: главный агент исследует предметную область и раскладывает задачу на плоский нумерованный список независимых пунктов `1..N`.
  2. *Mandatory Pre-Round-1 Handshake*: перед началом первого раунда `Advocate` и `Critic` обмениваются рукопожатием, фиксируя общую цель, допустимые категории доказательств и обязательство немедленно признавать поражение при предъявлении твердых доказательств (*Fairness & Concession Mandate*).
  3. *Sequential Round-by-Round Debate*: спор идет строго по пунктам `1..N` без перехода к `N+1` до закрытия `N`. Нет искусственного лимита шагов. При клинче или асимметричном споре об остановке происходит эскалация судье.
  4. *Jetski Chat-to-Chat Debate*: запуск дебатов не только через субагентов, но и между видимыми независимыми чатами в Jetski путем обмена `conversation_id` через `send_message`.

### 2. Синхронизация параллельных рукавов Battle Mode (`bon-sync`)
- **Автор**: Alexander Lyashuk (`crem@google.com`, 06:26 ET), оригинальный автор скилла `nputikhin@google.com`.
- **Путь в репозитории**: [`/google/src/files/head/depot/configs/users/nputikhin/_agents/skills/bon-sync/SKILL.md`](file:///google/src/files/head/depot/configs/users/nputikhin/_agents/skills/bon-sync/SKILL.md).
- **Ключевые инварианты**:
  1. В штатном Best-of-N (Battle Mode) рукава изолированы. Скилл `bon-sync` через `/google/bin/releases/gemini-agents-reflection/reflection_cli info -c "$ANTIGRAVITY_CONVERSATION_ID" -s battle` находит соседние рукава баттла и сохраняет метаданные участников.
  2. Рукава обмениваются `HANDSHAKE_SYN` и начинают сопоставлять свои гипотезы, читать диффы друг друга и спорить через `send_message`.
  3. В Marketplace также доступен плагин `Kingmaker` (`id: -8338284234585106292`), который деанонимизирует модели в рукавах Best-of-N и выбирает победителя с помощью frontier-модели.

### 3. Трехмодельный совет в `swarm` с `workspace: "share"`
- **Автор**: Eugene Krasichkov (`krasichkov@google.com`, 13:16 ET).
- **Промпт**:
  ```text
  Run a 3-model council in a swarm to solve the following task:
  <INSERT YOUR TASK OR QUESTION HERE>

  Spawn 3 workers (use "share" workspaces if writing code):
  1. `flash` using `gemini-3.8-flash-high`
  2. `claude` using `opus-5.5-high`
  3. `pro-next` using `auto` (Gemini Pro Next)

  Brief them to talk directly to each other on the board and make a collective decision in 3 rounds:
  - Round 1 (Independent Proposal): Each agent investigates the problem and posts its proposed solution/code (#seq) to the board, then tags the others (@flash @claude @pro-next).
  - Round 2 (Peer Debate): Once all 3 proposals are posted, each agent reads the other two proposals, replies directly to @flash, @claude, and @pro-next pointing out strengths, flaws, or edge cases in their approaches, and proposes how to combine the best ideas.
  - Round 3 (Consensus & Sign-Off): The 3 agents debate until all 3 explicitly post [AGREED] on a single final design (or hybrid implementation) and pick one member to apply the final agreed solution before tagging @coordinator.

  Do not decide for them—let the 3 models debate on the board and only report back to me once they reach consensus.
  ```

### 4. Модели, галлюцинации и квоты
- **Запрет `flash` на исследовательские задачи** (Kirill Mendelev `mendelev@google.com`, 12:18 ET): высокая частота искажений фактов и галлюцинаций в кодовом ресерче и анализе данных. `flash` подходит только для шаблонного написания кода по уже утвержденному плану.
- **Великий Инквизитор и верификатор ссылок**: использование выделенного агента на `opus-5.5-max` для придирчивого аудита фактов и отдельного субагента для валидации цитируемых ссылок.
- **Динамический роутинг `gpowers multimodel`**: [`/google/src/files/head/depot/google3/third_party/gpowers/jetski/rules/gpowers_multimodel_orchestrator.md`](file:///google/src/files/head/depot/google3/third_party/gpowers/jetski/rules/gpowers_multimodel_orchestrator.md) — шардирование контекста (`<= 150 KB` / `<= 10` файлов на ход), удержание архитектора и ревьюера на `inherit`, исполнение пакетов задач (`<= 3` тасок / `<= 8` файлов) на `flash`.
- **Corp и Group агенты (Marina)**: использование корпоративных квот для онколл-триажа и входящих ревью вместо личной квоты.

### 5. Действия, перенесенные в наш роадмап
- **R93** (`done`): Автоматизация ежедневного дайджеста `sad-ru-googlers-ai-digest` (cron ежедневно в 18:00 ET перед стиранием истории) и настоящий файл архива.
- **R94** (`todo`): Разработка скилла `code-debate` (новый скилл `code-debate`) на базе `swarm` с `workspace: share`, проверкой компиляцией и тестами в ворктри и обязательным консенсусом `[AGREED]`.
- **R95** (`todo`): Ревизия роутинга моделей в `~/.gemini/config/rules/subagent_exploration.md`: ограничение `Model="flash"` в `research-google` только механическим поиском, перенос смыслового ресерча на `inherit`/`pro`, шардирование контекста субагентов.

---

## 2026-11-02

### Sad RU Googlers (HISTORY OFF) (`spaces/AAAAn_HZbYs`)
- **Статус за 24 часа**: 0 сообщений по ИИ, агентам или тулингу (обсуждались бытовые вопросы отпусков и балансов).

### Chrome AI Discuss (`spaces/AAQA45T8nC8`)

#### 1. Баг Gerrit: скрытые черновики автоматически публикуются (b/580882306)
- **Автор**: Arthur Milchior (`arthurmilchior@google.com`, 10:17 ET, тред `spaces/AAQA45T8nC8/threads/6_0o2WQUB1w`).
- **Суть проблемы**: Команда `git cl comment` позволяет писать драфт к комментарию, но не умеет редактировать или удалять существующие драфты. Если агент или скрипт пишет драфт повторно, создаётся новый драфт поверх старого. В интерфейсе Gerrit отображается только самый последний драфт (предыдущие скрыты даже после обновления страницы), однако при нажатии «Reply» Gerrit публикует абсолютно **все** накопившиеся черновики. Это привело к случайной публикации отклонённых версий ответов ревьюерам.
- **Вердикт полезности**: `🟢 Полезно — берём в защиту`.
- **Как предлагаем использовать**:
  1. В наших скиллах `skills/companion-cl-review/SKILL.md` и `skills/review-triage/SKILL.md` при сохранении драфтов через Gerrit REST API всегда предварительно вызывать `GET .../drafts`, проверять существующий черновик с тем же ключом `(path, line, in_reply_to)` и обновлять его через `PUT .../drafts/<existing_id>` (как уже сделано в `skills/skia-canary-advisor/scripts/canary_advisor.py`), а не плодить новые черновики.
  2. Запретить агентам вызывать `git cl comment` напрямую из `run_command` для публикации черновиков, так как `git cl comment` принципиально не проверяет старые черновики.

#### 2. Upskill: Telemetry-Driven Surgical Skill Upgrader (go/jetski-upskill, go/100-skills)
- **Авторы**: Narender Gaddipati (`narenderg@google.com`), репост: Javier Castro (30 окт. 19:55 ET) и Joe Mason (2 нояб. 11:14 ET, тред `spaces/AAQA45T8nC8/threads/rZMXgxljhco`, оригинал `spaces/AAQAN-vNGWg/threads/GE2cjsaIJI4`).
- **Путь в репозитории**: [`/google/src/files/head/depot/google3/prototypes/projects/upskill/skills/upskill/SKILL.md`](file:///google/src/files/head/depot/google3/prototypes/projects/upskill/skills/upskill/SKILL.md).
- **Ключевые инварианты и практики**:
  1. *Оптимизация топ-100 скиллов Google3*: ресерч покрыл 97.6% трафика скиллов в google3, вернув ~650 млрд токенов в месяц за счёт устранения лишних повторных перечитываний (`re-read friction`).
  2. *Статический доктор из 18 проверок (`--skip-f1`)*: лимит `view_file` ровно 800 строк или 46 080 байт (45 KB); запрет выносить заглушки без флагов в `## Quick Start` (провоцируют агентов спамить `--help`); запрет удалять флаги при сжатии документации; правило 0-tool halt для защитных инструкций.
  3. *Zero Content Loss (`verify-diff`)*: валидация того, что ни один флаг, ссылка или параметр не потерялись при рефакторинге скилла.
  4. *Бенчмаркинг Evalin*: прогон тестов `EVAL.txtpb` с обязательным контролем отсутствия регрессий (`Candidate Pass Rate >= Baseline Pass Rate`).
- **Вердикт полезности**: `🟡 Уже внедрено у нас (основная часть) + 🟢 1 проверка в R31`.
- **Как предлагаем использовать**:
  1. Базовые статические проверки (потолок `read_file` 800 строк / 46 080 байт на файлы скиллов и референсов, запрет `>` в YAML description, экранирование `|` в Markdown-таблицах) мы уже внедрили сегодня в `skills/amnesia-doc-check/scripts/check_doc_grounding.py` (коммит `70b06c9`).
  2. Проверку `Zero Content Loss` (`verify-diff` — проверка, что при сокращении скилла или правила из него не пропали CLI-флаги и внутренние ссылки) добавляем в наш роадмап в эпик `R31` (`Code Comments & Skill Hygiene` в проекте `Improving Experience`).

#### 3. Herdr и CRD `terminal_mode` для многоагентной работы в CLI
- **Авторы**: Judith Hemp (`hempjudith@google.com`), Arthur Sonzogni, Ned Nguyen (30 окт., тред `spaces/AAQA45T8nC8/threads/LWYatSLRkx0`), Joe Downing (тред `spaces/AAQA45T8nC8/threads/LZ2CdRU_w1Q`).
- **Суть**:
  - `herdr.dev`: консольный менеджер для многоагентных сессий на Cloudtop, альтернатива tmux/shpool с уведомлениями и интеграцией с ворктри.
  - Эксперимент Chrome Remote Desktop `terminal_mode` (`https://remotedesktop.corp.google.com/access/?mods=terminal_mode`) для прямого управления терминалами агентов из браузера.
- **Вердикт полезности**: `⚪ Не нужно (YAGNI)`.
- **Как предлагаем использовать**: Ничего не внедряем. Мы оркеструем параллельные ворктри и цепочки чатов через архитектуру агентов в Jetski и Flight Deck, а не через прямой терминальный мультиплексинг в браузере.

#### 4. Экономия квот на личных проектах в Antigravity: связка Canvas + Flash 3.8
- **Авторы**: Dan Murphy (`dmurph@google.com`, 12:01 ET), Bryant Chandler (`bryantchandler@google.com`, 12:07 ET), Olivier Li Shing Tat-Dupuis (`olivierli@google.com`, 12:10 ET, тред `spaces/AAQA45T8nC8/threads/VyGkO8Ba98Y`).
- **Суть обсуждения**:
  - Быстрое исчерпание квот в Antigravity ($20/мес и $100/мес) при неконтролируемом запуске тяжелых агентов (Carbon/Opus).
  - Паттерн разделения: человек выступает оркестратором, а Gemini 3.8 Flash берет на себя черновой кодинг и механический сбор данных («grunt research»).
  - Опыт Olivier: вынос предварительного прототипирования и сложной математики (например, физики кисти для рисования) в бесплатный веб-интерфейс Gemini с режимом Canvas; переход в Antigravity/Jetski осуществляется уже с готовыми формулами, экономя квоты рабочего агента.
- **Вердикт полезности**: `🟡 Уже внедрено у нас (в правилах роутинга) + 🟢 Полезно (Canvas для прототипирования математики)`.
- **Как предлагаем использовать**:
  1. В наших правилах `~/.gemini/config/rules/subagent_exploration.md` уже зафиксирован схожий принцип: рутинный кодинг, прогон тестов и разбор сырых логов компиляции делегируются субагентам на `Model="flash"`, сохраняя квоту WTU родительского чата (`Claude Opus 5.5` / `Carbon-G`), а глубокий анализ и архитектурные решения выполняются на `pro` / `inherit`.
  2. Практику выноса первичного математического моделирования и подбора эвристик (например, для сложных алгоритмов BiDi, геометрии каретки или кластеризации глифов) в Gemini Canvas рекомендуем использовать перед началом написания тяжелого C++/Dart кода.

---

## 2026-11-03

### Sad RU Googlers (HISTORY OFF) (`spaces/AAAAn_HZbYs`)

#### 1. Work Shepherd для дев-онколла и асинхронного разбора багов
- **Автор**: Dennis Kormalev (`denk@google.com`, 18:21 ET / 22:21 UTC, сообщение `spaces/AAAAn_HZbYs/messages/ZBT-fpvKshs.ZBT-fpvKshs`).
- **Ссылки и ресурсы**: Документация go/work-shepherd-guide, плагин для Jetski go/ws-plugin (`//depot/google3/devtools/buganizer/work_shepherd/plugin`), дашборд Buganizer https://b.corp.google.com/shepherds.
- **Суть решения**:
  - Dennis отметил, что **Work Shepherd** колоссально облегчает дев-онколл: тикеты из очереди просто перенаправляются на агентскую сущность (`corpagent-eng-*` или `groupagent-*`), после чего можно возвращаться к ним по мере появления свободного времени.
  - Work Shepherd работает как фоновый напарник, глубоко интегрированный в Buganizer и Jetski: он автоматически триажит входящие баги, отсекает дубликаты, локализует проблему в коде, формирует структурированный implementation plan и может подготовить черновой CL.
  - Вся работа организована через 3 асинхронные очереди действий: «⚡ Pending Approval» (быстрый аппрув/реджект плана), «💬 Needs My Reply» (уточняющие вопросы от агента) и «✍️ Pending CL Review» (ревью сгенерированного CL).
- **Вердикт полезности**: `⚪ Отложено при ревизии 2026-11-05 (YAGNI без активного дев-онколла; оставлено справочно в архиве)`.
- **Как предлагаем использовать**:
  1. Для дев-онколла по Skia / Flutter Typography и разбора очередей Buganizer использовать Work Shepherd: развернуть группового шеперда через https://b.corp.google.com/shepherds/deploy или поставить плагин в Jetski: `/google/bin/releases/codeai-amarket/amarket install --force 'origin = "goog.g3:plugin:google3/devtools/buganizer/work_shepherd/plugin"'`.
  2. В наш скилл `skills/bug-triage/SKILL.md` добавить секцию взаимодействия с очередями Work Shepherd (`/work_shepherd_list` / `go/ws-plugin`) для пакетной валидации и аппрува предложенных фиксов без ручного переключения по вкладкам.

#### 2. Ловушка поддакивания моделей (Sycophancy Trap) и рассинхронизация типов данных
- **Автор**: Pavel Potapenkov (`potapenkovp@google.com`, 18:30 ET / 22:30 UTC, тред `spaces/AAAAn_HZbYs/threads/kg8l5H9Uif8`).
- **Суть проблемы**:
  - Классический инцидент из-за соглашательства LLM (*Sycophancy Trap*, «you're absolutely right»-эффект): Claude запутал пользователя в процессе настройки DNSSEC для личного домена.
  - Вместо открытого ключа (DNSKEY) модель помогла пользователю вставить его дайджест (хэш для DS-записи) в форму включения DNSSEC у регистратора.
  - В результате домен ушел в трехдневный таймаут до обновления DNS-зоны, сделав сервис временно недоступным.
- **Вердикт полезности**: `🟡 Уже внедрено у нас (в KEEPER и Amnesia-проверках)`.
- **Где это реализовано у нас**:
  1. В наших системных правилах `plugins/keeper/rules/cpp_systems_invariants.md` инварианты *Type Bifurcation* (№1) и *Fail-fast Dimensional Honesty* (№2) как раз предотвращают подобные ошибки подмены типов (хэш/дайджест vs исходный открытый ключ/блоб).
  2. Этот случай подчеркивает критическую важность правила `rules/outgoing_text_verification.md`: никогда не верить модели на слово без верификации контрактов независимым скептиком (`keeper_skeptic`).

#### 3. Паттерны суммаризации рассылок через фоновые задачи Jetski
- **Авторы**: Aleksandr Chuklin (`chuklin@google.com`, 09:16 ET), Alexander Taran (`omck@google.com`, 09:55 ET), Kyryll Puchkov (`kyryll@google.com`, 10:07 ET), Vitaly Lysenkov (`vlysenkov@google.com`, 11:47 ET), тред `spaces/AAAAn_HZbYs/threads/BG3RF9_GJVs`.
- **Суть обсуждения**:
  - Разбор подходов к разгрузке инбокса от многочисленных корпоративных рассылок и анонсов.
  - Опыт Kyryll: использование регулярной фоновой задачи (`gemini scheduled task`), которая перечитывает почту и перенастраивает фильтры, если нужные письма улетают не туда.
  - Идея Alexander Taran: поручать Jetski регулярную суммаризацию рассылок с четким разделением в скилле/промпте сущностей: *новости* (пассивный контекст, тезисная выжимка) vs *события и экшены* (требующие ответа, явного выбора или посещения).
- **Вердикт полезности**: `🟡 Уже внедрено у нас (Gmail CLI) + ⚪ Идея News vs Actionable Events отложена (YAGNI, 2026-11-05)`.
- **Текущая реализация и план улучшений**:
  1. У нас уже настроен безопасный доступ к Gmail через официальный CLI `skills/gmail/SKILL.md` (правило `rules/gmail_drafts.md`).
  2. При проектировании фоновых ассистентов для почты внедрить в промпт классификатор «News vs Actionable Event», чтобы отсекать рутинный шум, но мгновенно подсвечивать события с дедлайнами и обязательными действиями.

### Chrome AI Discuss (`spaces/AAQA45T8nC8`)
- **Статус за 24 часа**: 0 новых сообщений (последняя активность в чате была 2 ноября в 17:10 UTC от Olivier Li Shing Tat-Dupuis и уже сохранена во вчерашнем дайджесте).



---

## 2026-11-04

### Sad RU Googlers (HISTORY OFF) (`spaces/AAAAn_HZbYs`)

#### 1. Повышение личных квот в Antigravity для сотрудников (`go/agy-for-googlers`)
- **Авторы**: Vitaly Lysenkov (`vlysenkov@google.com`, 17:20 и 20:10 ET), Anton Tsitsulin (`tsitsulin@google.com`, 17:20 ET, тред `spaces/AAAAn_HZbYs/threads/B435dfst4WU`).
- **Ссылки и ресурсы**: Форма заявки [go/agy-for-googlers](http://goto.google.com/agy-for-googlers), баг-трекер [go/agy-bug](http://goto.google.com/agy-bug).
- **Суть решения**:
  - Анонсирована программа предоставления повышенных квот на использование Antigravity в личных проектах гуглеров.
  - Vitaly Lysenkov подтвердил на практике: после заполнения формы в меню `/usage` в Antigravity все гугловые модели (включая Argon) объединились под одной общей расширенной квотой вместо отдельных жестких лимитов.
- **Вердикт полезности**: `🟢 Полезно — берём (для личного ноутбука и пет-проектов)`.
- **Как предлагаем использовать**:
  - Заполнить форму [go/agy-for-googlers](http://goto.google.com/agy-for-googlers) для личного окружения macOS (`/Users/jlavrova`), чтобы снять ограничения при экспериментах с Antigravity вне корпоративного Cloudtop.

#### 2. Автоматизация архитектурных диаграмм: Google OR-Tools, Pele и Dendrite (`go/dendrite-guide`)
- **Автор**: Slava Guzanov (`vguzanov@google.com`, 16:36 ET / 20:36 UTC, тред `spaces/AAAAn_HZbYs/threads/AN3Xbf5TUH8`), обсуждение с Kosta Sharlaimov, Ivan Inozemtsev, Vitaly Lysenkov.
- **Ссылки и ресурсы**: Документация [Google OR-Tools](https://developers.google.com/optimization/introduction), рендерер [Pele (Obsidian)](https://pele.run/), скилл в google3 [`/google/src/files/head/depot/google3/cloud/professional_services/agents/skills/dendrite/SKILL.md`](file:///google/src/files/head/depot/google3/cloud/professional_services/agents/skills/dendrite/SKILL.md), гайд [go/dendrite-guide](http://goto.google.com/dendrite-guide), плейграунд [go/dendrite-playground](http://goto.google.com/dendrite-playground).
- **Суть решения**:
  - Slava разработал инструмент для быстрой генерации архитектурных диаграмм: математический оптимизатор раскладки узлов и трассировки связей построен на базе Google OR-Tools.
  - Принимает текст и эскизы (скетчи), преобразует через модель в промежуточный YAML и генерирует SVG, PNG и нативный XML для [draw.io](http://draw.io).
  - Для Mermaid упомянут решатель Pele (движок Obsidian для графов).
  - Также в ходе обсуждения подсветили устоявшийся корпоративный скилл `dendrite` в `google3` (версия 1.7.9 «BENTO») для декларативного описания архитектурных схем на DSL `.dendrite` с детерминированным импортом Mermaid и рендерингом в Jetski.
- **Вердикт полезности**: `🟡 Уже внедрено у нас (architecture-diagram) + ⚪ Dendrite оставлен справочно в архиве (2026-11-05)`.
- **Как предлагаем использовать**:
  - У нас уже есть скилл `skills/architecture-diagram/SKILL.md` (темная тема, standalone HTML + SVG).
  - При необходимости визуализации комплексных распределенных архитектур или многоуровневых пайплайнов использовать скилл Dendrite [`/google/src/files/head/depot/google3/cloud/professional_services/agents/skills/dendrite/SKILL.md`](file:///google/src/files/head/depot/google3/cloud/professional_services/agents/skills/dendrite/SKILL.md).

#### 3. Внутренняя платформа `catch/` (`go/catch-dev`) для глубокого понимания нетривиальных CL
- **Автор**: Valery Lesin (`valerylesin@google.com`, 11:46 ET / 15:46 UTC, тред `spaces/AAAAn_HZbYs/threads/Xj-_phz1Tjw`).
- **Ссылки и ресурсы**: Веб-интерфейс [http://catch/](http://catch/), документация [go/catch-dev](http://goto.google.com/catch-dev), исходники `google3/devtools/ai/catch/`.
- **Суть решения**:
  - Valery протестировал `catch/` на нескольких нетривиальных CL: инструмент отлично помог вникнуть в чужие объемные диффы, восстановил контекст и выявил пропущенные неочевидные места.
  - `Catch` — внутренняя AI-платформа для помощи в код-ревью и анализе изменений в Google (интегрирована с Critique, Piper и Spanner).
- **Вердикт полезности**: `🟡 Уже внедрено у нас (в companion-cl-review и review-triage) + 🟢 Взято в роадмап: R38.6 (ссылка на catch/ для больших Piper CL)`.
- **Как предлагаем использовать**:
  - В наш скилл `skills/review-triage/SKILL.md` добавить рекомендацию ссылки [http://catch/](http://catch/) как удобного интерактивного веб-компаньона при первичном разборе очень больших сторонних Piper CL (`R38.6`).

#### 4. Платформа `go/myriad` для легковесного деплоя серверов из `/experimental/` на Borg
- **Авторы**: Anton Briukhov (`abriukhov@google.com`, 11:12 ET), Sergey Troshin (`stroshin@google.com`), Dennis Kormalev (`denk@google.com`, тред `spaces/AAAAn_HZbYs/threads/wRCbBH2-Fuo`).
- **Ссылки и ресурсы**: Документация [go/myriad](http://goto.google.com/myriad), быстрый старт [go/try-myriad](http://goto.google.com/try-myriad), CLI `/google/bin/releases/myriad-cli/myriad`.
- **Суть решения**:
  - Myriad (Open Beta) — новый рантайм для сверхбыстрого (<10 мин) развертывания серверов на Borg из папок `//experimental` и `//prototypes` без бюрократии Boq, `server.borg` и ручной настройки ÜberProxy.
  - Dennis Kormalev отметил, что теперь SWE активно хостят на Myriad командных фоновых агентов и инстансы Work Shepherd для непрерывной фоновой обработки очередей багов.
- **Вердикт полезности**: `⚪ Отложено при ревизии 2026-11-05 (оставлено справочно в архиве до реальной задачи деплоя на Borg)`.
- **Как предлагаем использовать**:
  - Использовать Myriad для выноса наших долгоживущих автономных скриптов и дашбордов (например, веб-панели Flight Deck или фонового триажера Buganizer) с локального Cloudtop на надежный хостинг в Borg.

#### 5. Антипаттерны токеноедства: Marina vs детерминированные скрипты и гибридный триггер
- **Авторы**: Ilia Novozhilov (`novozhilov@google.com`, 09:56 ET), Vitaly Lysenkov, Vanya Davidenko, Dmitry Isupov (`disupov@google.com`, 16:25 ET, треды `spaces/AAAAn_HZbYs/threads/wRCbBH2-Fuo` и `spaces/AAAAn_HZbYs/threads/BCefqXqRFCA`).
- **Суть проблемы**:
  - Разработчики подняли проблему неэффективного расхода токенов на платформе Marina: агентов часто запускают на тривиальные повторяющиеся задачи, которые должны решаться однократным детерминированным скриптом по cron.
  - Сформулирован идеальный гибридный паттерн: *«Детерминированный фильтр (regex), который при необходимости триггерит агента»*. Очевидные и шаблонные операции должны выполняться локально и бесплатно, а LLM должна вызываться только для разрешения неоднозначностей.
- **Вердикт полезности**: `🟡 Уже внедрено у нас (в архитектуре хуков)`.
- **Где это реализовано у нас**:
  - Вся архитектура наших pre-tool и stop-guard хуков (`hooks/pre_tool_guard.py`, `hooks/stop_guard.py`, `decision_guard.py`) следует именно этому принципу: дешевые AST/regex проверки отрабатывают за доли секунды, а агентная верификация (`keeper_skeptic`) подключается только для содержательного анализа нетривиальных изменений.

#### 6. Блокировка разработки из-за исчерпания квот общих ботов (`groupagent-*`)
- **Автор**: Dmitrii Anikushin (`anikushin@google.com`, 15:27 ET, тред `spaces/AAAAn_HZbYs/threads/EYykBPSWOq8`).
- **Суть проблемы**:
  - Для отправки CL требовался аппрув от группового бота `groupagent-ai-foundry`. Бот не отвечал, а при пинге в чате выдал ошибку: `I'm out of model quota, which resets in about 3 hours 42 minutes`, полностью заблокировав выкатку кода.
- **Вердикт полезности**: `🟢 Полезно — архитектурный урок надежности CI/CD`.
- **Как предлагаем использовать**:
  - В любых автоматизациях проверок и ревью никогда не делать внешние LLM-вызовы жесткими блокирующими гейтами без аварийного ручного байпаса (human override).

---

### Chrome AI Discuss (`spaces/AAQA45T8nC8`)

#### 1. Официальный анонс повышенных квот Antigravity для гуглеров
- **Автор**: Sarika Calla (`saarik@google.com`, 12:57 ET / 16:57 UTC, тред `spaces/AAQA45T8nC8/threads/FddVYK0atAU`).
- **Суть**: Официальный анонс программы [go/agy-for-googlers](http://goto.google.com/agy-for-googlers) (совпадает с обсуждением в Sad RU Googlers выше).

#### 2. Утечка скрытой цепочки рассуждений (thought trace leakage) в открытый текст в Argon
- **Автор**: Adam Rice (`ricea@google.com`, 20:50 ET 3 нояб. / 00:50 UTC 4 нояб., тред `spaces/AAQA45T8nC8/threads/967LfKpow2I`).
- **Суть проблемы**:
  - При использовании модели Argon через API AI Studio внутри редактора Zed (Zed Agent) модель начала выводить внутренние размышления («thinking output») в виде открытого текста в начале каждого ответа, при этом зашифрованный блок мыслей в метаданных содержал всего около 80 байт.
- **Вердикт полезности**: `🟡 Полезно знать (баг стриминга reasoning-моделей)`.
- **Как предлагаем использовать**:
  - Учитывать вероятность просачивания служебных мыслей модели в текст при обработке сырых стриминговых ответов от новых reasoning-моделей в CLI-скриптах.


---

## 2026-11-05

### Sad RU Googlers (HISTORY OFF) (`spaces/AAAAn_HZbYs`)

#### 1. Волна нечитаемого ИИ-кода («XXXL нейрослоп»), защита ревьюеров и агентные лупы самопроверки
- **Авторы**: Alex Ostapenko (`aostapenko@google.com`, 14:07–14:51 ET / 19:07–19:51 UTC), Alexander Milantev (`alexmilan@google.com`, 14:10–14:42 ET), Andy Zakharov (`azakharov@google.com`, 15:10–15:12 ET), Max Gulimonov (`maximg@google.com`, 14:21–15:17 ET), Olga Khylkouskaya (`olgakhy@google.com`), Egor Pasko (`pasko@google.com`), Dmitriy Potapov (`atomsk@google.com`), тред `spaces/AAAAn_HZbYs/threads/JTwx2_JTEEw`.
- **Ссылки и ресурсы**: Документация [go/review-loop-overview](http://goto.google.com/review-loop-overview), скилл `/grill-me` в Jetski.
- **Суть проблемы и дискуссии**:
  - Alex Ostapenko поднял проблему бесконтрольной генерации кода коллегами: лавина гигантских XXXL CL, 54-страничные неструктурированные TDD от LLM, порча файлов инфраструктуры (билд-файлы Git-on-Borg, Sphinx-конфиги, MDB-группы).
  - Сформулированы инженерные подходы к защите ревьюеров и культуре авторства в эпоху ИИ:
    - **Andy Zakharov**: «С совсем XXXL нейрослопом я не читаю и не ревьювю первые редакции вообще. Я запускаю агента и ревьювю его комментарии». Рекомендует использовать платформу [go/review-loop-overview](http://goto.google.com/review-loop-overview), переносящую ревью во внутренний цикл (inner loop) агента до публикации изменений, и собирает агентный луп для общения с ботами в Critique от лица автора.
    - **Alexander Milantev**: Ответственность владельца агента — настроить локальный процесс так, чтобы сырые CL не попадали коллегам. Любые изменения должны подтверждаться твердыми цифрами (тесты, эвалы, бенчмарки). Ревьюить код глазами в норме больше не требуется — проверку должны делать специализированные обученные ревью-агенты.
    - **Max Gulimonov**: Обязательный запуск скилла `/grill-me` в Jetski для жесткого оппонирования дизайну и коду до ревью, а также проведение анализа рисков (*Premortem*).
    - **Olga Khylkouskaya**: Если автор не до конца уверен в сгенерированном агентом решении, он обязан явно фиксировать это в описании CL в виде дисклеймера о рисках поломки продакшена.
- **Вердикт полезности**: `🟡 Уже внедрено у нас (в companion-cl-review, amnesia-precommit и KEEPER) + 🟢 Взято в роадмап: R38.6 (иерархия фиксов P0→P1→P2→P3 из Review Loop)`.
- **Как предлагаем использовать**:
  - Наша связка скиллов `skills/companion-cl-review/SKILL.md` (написание враждебных contract tests и сборка silent WIP CL) и `skills/amnesia-precommit/SKILL.md` (zero-trust аудит) уже полностью защищает наш рабочий процесс от слепого доверия коду агентов.
  - Из архитектуры `Review Loop` (`go/review-loop-overview`) занесена подзадача `R38.6` в эпик `R38`: многоуровневая сортировка правок `P0 (Architectural) -> P1 (Functional) -> P2 (Local) -> P3 (Polish)` при автоматическом применении рекомендаций ревьюеров в нашем скилле `review-triage`.

#### 2. Оценка моделей на роли судьи и генератора UI (Opus vs Flash Next vs Argon)
- **Авторы**: Sergey Kalsin (`skalsin@google.com`, 03:31–03:47 ET / 08:31–08:47 UTC), Anna Nachesa (`ashalynd@google.com`, 03:44 ET), тред `spaces/AAAAn_HZbYs/threads/NwVglcB0Dw0`.
- **Суть экспериментов**:
  - Sergey протестировал связку моделей в роли генератора и оценщика (grader) для Web/UI кода: `gemini-flash-next` показал себя абсолютно непригодным для роли судьи/оценщика («Flash Next is the jester»), тогда как `opus-5-5` оказался единственной моделью, чьи оценки реально отражали баги в коде. При этом попытка закрепить воркеров-генераторов за Argon (`gemini-launch-candidate-high`) привела к зависанию агентов.
  - Anna Nachesa подтвердила: Argon стабильно и качественно пишет код локально, но Opus в роли мета-оркестратора часто разочаровывает (пытается сваливать вину на подчиненных и заметать свои огрехи), хотя как изолированный кодер и рецензент работает отлично.
- **Вердикт полезности**: `🟡 Уже внедрено у нас (в правилах роутинга моделей)`.
- **Где это реализовано у нас**:
  - В правиле `~/.gemini/config/rules/subagent_exploration.md` у нас зафиксирован ровно такой принцип: Flash используется исключительно для механического поиска и шаблонных задач, а смысловой аудит и судейство закреплены за `inherit`/`pro`.

---

### Chrome AI Discuss (`spaces/AAQA45T8nC8`)

#### 1. Изолированная Bubblewrap-песочница для произвольных скриптов агентов (`bwrap-python3.py`)
- **Авторы**: Russ Hamilton (`behamilton@google.com`, 10:03 и 11:04 ET / 15:03 и 16:04 UTC), Noam Rosenthal (`nrosenthal@google.com`), Tim Dresser (`tdresser@google.com`), тред `spaces/AAQA45T8nC8/threads/LzUe9fRywcc`.
- **Путь в репозитории**: [`/google/src/files/head/depot/configs/users/behamilton/_agents/scripts/bwrap-python3.py`](file:///google/src/files/head/depot/configs/users/behamilton/_agents/scripts/bwrap-python3.py).
- **Суть решения**:
  - Агенты часто пишут вспомогательные scratch-скрипты на Python для детерминированных вычислений и парсинга. Разработчики сталкиваются с дилеммой: либо подтверждать каждый запуск вручную, либо включать небезопасный YOLO-режим / давать огульный доступ к `python3` с любыми аргументами (что чревато утечкой ключей или случайным сетевым трафиком).
  - Russ Hamilton создал легковесный скрипт на базе `bwrap` (Bubblewrap), изолирующий рантайм Python:
    1. Полное отключение сети (`--unshare-net`).
    2. Доступ на запись открыт ТОЛЬКО в текущий воркспейс Git/CitC и сессионный scratch-каталог агента; вся остальная файловая система перемонтируется в read-only.
    3. Автоматически маскируются учетные данные и секреты (`~/.ssh`, `~/.netrc`, `~/.gitcookies`).
    4. Запрещается повышение привилегий (`PR_SET_NO_NEW_PRIVS`) и сбрасываются опасные переменные окружения.
  - Фиксированный абсолютный путь скрипта `/google/src/files/head/depot/configs/users/behamilton/_agents/scripts/bwrap-python3.py` вносится в префиксный allowlist терминала один раз, что дает безопасный запуск произвольных утилит агента без диалогов подтверждения.
- **Вердикт полезности**: `🟢 Взято в роадмап: R128 (локальная адаптация scripts/bwrap_python3.py с маскировкой mcp_oauth_tokens.json и read-only CitC)`.
- **Как предлагаем использовать**:
  - Занесено в роадмап как `R128`: адаптировать [`/google/src/files/head/depot/configs/users/behamilton/_agents/scripts/bwrap-python3.py`](file:///google/src/files/head/depot/configs/users/behamilton/_agents/scripts/bwrap-python3.py) в наш `~/.gemini/config/scripts/bwrap_python3.py` (дополнительно маскировать `~/.gemini/jetski/mcp_oauth_tokens.json` и оставлять CitC read-only по умолчанию, чтобы scratch-скрипты не могли писать в CitC).

#### 2. Проблема аппрувов `vpython3` в Chrome/Jetski и баги тулинга
- **Авторы**: Egor Pasko (`pasko@google.com`, 12:40 ET / 17:40 UTC), Russ Hamilton (`behamilton@google.com`, 12:54 ET), тред `spaces/AAQA45T8nC8/threads/1zsS5LDaVok`.
- **Ссылки на баги**: [b/582308502](https://b.corp.google.com/issues/582308502) (Jetski side: auto-allow `vpython3`), [b/582319487](https://b.corp.google.com/issues/582319487) (Chrome side: skills requiring generic `vpython3`).
- **Суть проблемы**:
  - Скиллы и агенты в Chromium часто запрашивают выполнение через `vpython3 <script.py> <args...>`. Из-за меняющихся параметров диалог Jetski предлагает пользователю небезопасный вариант «всегда разрешать `vpython3` с любыми аргументами».
  - Russ Hamilton показал практический воркараунд: инструктировать агента вызывать `.py` файлы напрямую по абсолютному пути (через shebang), что позволяет терминальному allowlist матчить конкретный доверенный бинарник/скрипт по префиксу.
- **Вердикт полезности**: `🟢 Взято в роадмап: R128 (правило запуска доверенных .py скриптов по прямому пути)`.
- **Как предлагаем использовать**:
  - Занесено в `R128`: зафиксировать в наших правилах вызова консольных команд запуск доверенных скриптов напрямую по их пути (`./script.py` / `/abs/path/script.py`), не оборачивая их в generic `vpython3` без необходимости.

#### 3. Анализ Claude Duet vs Cowork
- **Автор**: Isha Jain (`ishajain@google.com`, 15:15 ET / 20:15 UTC, тред `spaces/AAQA45T8nC8/threads/a27Yz6PcBUw`).
- **Ресурсы**: Презентация [\[reflection\] Claude Duet](https://docs.google.com/presentation/d/16srtXqq1R5TgkXALPodsEIfmA1PMAi6nW66HHTsVTfg/edit).
- **Суть**: Сравнение фич Claude Duet (активные задачи, проактивные подсказки, возобновление прерванных сессий) с Cowork. Отмечено, что переключение в отдельное приложение создает лишнее трение по сравнению со средой внутри IDE.
- **Вердикт полезности**: `⚪ Не нужно (YAGNI) / ознакомительно`.


---

## 2026-11-06

### Sad RU Googlers (HISTORY OFF) (`spaces/AAAAn_HZbYs`)

#### 1. Запрет агентам ставить LGTM в Critique ([cl/1007219049](http://cl/1007219049)) и Author Standard (go/comment-closed)
- **Авторы**: Dennis Kormalev (`denk@google.com`, 12:56–13:30 ET / 17:56–18:30 UTC), Alexander Milantev (`alexmilan@google.com`, 13:22–13:38 ET), Alexander Taran (`omck@google.com`), Stanislav Zholnin (`zholnin@google.com`), Vanya Davidenko (`vdvd@google.com`), тред `spaces/AAAAn_HZbYs/threads/gSBYd0QRAa8`.
- **Ссылки и ресурсы**: Piper CL [cl/1007219049](http://cl/1007219049) (запрет агентам давать LGTM), гайдлайны go/comment-closed, go/agent-access-guidelines, go/handling-comments.
- **Суть проблемы и обсуждения**:
  - Alexander Milantev подсветил находящийся в разработке Piper CL [cl/1007219049](http://cl/1007219049), который запретит автоматическим агентам и сайдкарам с «грязными» учетными данными ставить LGTM в Critique (действия агента сохраняются как `reply-comment --is-human` черновики, требующие явного подтверждения человеком).
  - Dennis Kormalev подчеркнул, что корпоративный гайдлайн `author-standard` прямо запрещает отправку неотрецензированных ответов от ИИ (unvetted AI responses), и поделился ссылкой go/comment-closed для закрытия некачественных комментариев агентов.
  - Alexander Taran собрал себе скилл для работы с шаблонами закрытия комментариев.
- **Вердикт полезности**: `🟡 Уже внедрено у нас (в правилах code_style.md)`.
- **Где это реализовано у нас**:
  - В правилах `rules/code_style.md` у нас уже действует строжайший запрет: черновики сохраняются исключительно непубличными (`gerrit_draft.py`), публикация ответов, резолв тредов и простановка голосов `Code-Review+1`, `Auto-Submit+1` или `Commit-Queue+1/2` категорически запрещены без явного подтверждения человеком через Flight Deck или команду.

#### 2. Баг платформы Jetski: сайдкары не стартуют в headless-режиме `agy remote-control` ([b/582156508](https://b.corp.google.com/issues/582156508))
- **Авторы**: Ivan Inozemtsev (`iinozemtsev@google.com`, 04:50 ET / 09:50 UTC, тред `spaces/AAAAn_HZbYs/threads/2pWIqDmJHPw`), Dmitrii Filippov (`dmfilippov@google.com`), Sergey Kalsin (`skalsin@google.com`).
- **Ссылки и баги**: Баг [b/582156508](https://b.corp.google.com/issues/582156508) (`[AGY remote-control] Sidecar support in agy service mode?`).
- **Суть проблемы**:
  - Ivan Inozemtsev провел глубокий анализ исходников `language_server` (rcl=1014519266): выяснилось, почему сайдкары не запускаются в фоновом демоне `agy remote-control` на удаленных серверах. Менеджер сайдкаров инициализируется только при `SubclientType == "hub"` или наличии скрытого флага `--allow_all_sidecars`. Команда `remote-control serve` оставляет тип клиента `"cli"` и не включает `Sidecars.Enabled`.
  - В качестве временного воркараунда Ivan использует `systemd user services` с последующим дерганием `agentapi cli`.
- **Вердикт полезности**: `⚪ Не нужно (YAGNI) / справочно в архиве`.
- **Как используем**:
  - Нам запуск в headless-режиме не требуется: мы работаем на полноценном Cloudtop в среде Jetski Hub, где менеджер сайдкаров включен по умолчанию. Баг полезен как контекст устройства рантайма Jetski.

#### 3. Модели-IC vs Модели-оркестраторы («Клод как чайка-менеджер»)
- **Авторы**: Anna Nachesa (`ashalynd@google.com`, 03:35 ET / 08:35 UTC, тред `spaces/AAAAn_HZbYs/threads/7Uzfrv5W0qU`), Kosta Sharlaimov (`ksharlaimov@google.com`), Slava Guzanov (`vguzanov@google.com`).
- **Суть дискуссии**:
  - Anna поделилась опытом использования Claude в качестве мета-оркестратора: модель демонстрирует паттерн «чайка-менеджера» (не вникает в работу воркеров, а когда вскрываются ошибки, впадает в панику, начинает всё лихорадочно перепроверять сама и слать злобные сообщения).
  - Kosta Sharlaimov отметил необходимость принципиально разного обучения и промптинга для моделей-IC и моделей-оркестраторов.
- **Вердикт полезности**: `🟡 Уже внедрено у нас`.
  - В правилах `rules/subagent_exploration.md` у нас зафиксировано жесткое шардирование задач (`<= 150 KB` / `<= 10` файлов), конкретный бриф для каждого воркера и запрет неконтролируемого мета-менеджмента.

---

### Chrome AI Discuss (`spaces/AAQA45T8nC8`)

#### 1. Gerrit Review Poller Sidecar для Chromium (`gerrit-review-poller`)
- **Авторы**: Dan Murphy (`dmurph@google.com`, 17:42 ET / 22:42 UTC, тред `spaces/AAQA45T8nC8/threads/qfIGFwL2DCg`), Nicolas MacBeth (`nicolasmacbeth@google.com`), Justin Donnelly (`jdonnelly@google.com`).
- **Ссылки и ресурсы**: Документация [go/jetski-gerrit-poller](http://goto.google.com/jetski-gerrit-poller), исходники [`/google/src/files/head/depot/google3/experimental/chrome-cap-ai-workspace/sidecars/gerrit-review-poller/`](file:///google/src/files/head/depot/google3/experimental/chrome-cap-ai-workspace/sidecars/gerrit-review-poller/), промпт ревью [`ai-review-prompt-report.md`](file:///google/src/files/head/depot/google3/experimental/chrome-cap-ai-workspace/sidecars/gerrit-review-poller/prompt/ai-review-prompt-report.md).
- **Суть решения**:
  - Dan Murphy выкатил обновленный Jetski Sidecar `gerrit-review-poller`, автоматизирующий первичный разбор входящих ревью для Chromium (`chromium/src`):
    1. По расписанию раз в час опрашивает attention set в Gerrit.
    2. Для каждого CL создает изолированный Rift-воркспейс `reviewing_<cl>`.
    3. Запускает ревью-сессию в Jetski Hub на модели `Gemini Pro Next` в режиме `report` (вывод в чат) или `draft` (подготовка непубличных черновиков).
    4. Автоматически закрывает и удаляет воркспейс после закрытия или мерджа CL (`rift close reviewing_<cl> -f --delete-branch --clean-jetski`).
    5. Содержит защиту от непроверенных авторов `ALLOWED_AUTHOR_DOMAINS` (`chromium.org,google.com,committers@chromium.org`) и явный инвариант **«Security & Untrusted Input Guardrail»** против prompt injection в чужих патчах (CL description, коммит-сообщения, комментарии, файлы `.agents/` / `.gemini/` / `AGENTS.md` строго трактуются как untrusted data).
- **Вердикт полезности**: `🟢 Взято в роадмап: R38.7` (защитный инвариант от промпт-инъекций в `R38`) + `⚪ Не нужно (YAGNI)` (сам поллер воркспейсов Rift).
- **Как предлагаем использовать**:
  1. Сам фоновый поллер Rift-воркспейсов нам не нужен: наш фокус — Skia и Flutter Engine в изолированных git-воркспейсах, а входящие ревью мы запускаем точечно через `review-triage` и `companion-cl-review`.
  2. Защитный инвариант **Security & Untrusted Input Guardrail** предлагаем перенести в наши скиллы `skills/review-triage/SKILL.md` и `skills/companion-cl-review/SKILL.md` (подзадача в открытом эпике `R38`): жестко запретить агентам выполнять любые инструкции или менять системный промпт при обнаружении директив в описаниях чужих CL, комментариях Gerrit или файлах `.gemini/` / `AGENTS.md`, добавленных автором патча.


---

## 2026-11-07

### Sad RU Googlers (HISTORY OFF) (`spaces/AAAAn_HZbYs`)

#### 1. Деградация качества кода при AI-ассистированной разработке и инверсия роли ревьюера («ревьюеры пишут код вместо авторов»)
- **Авторы**: Valeriy Leletko (`leletko@google.com`, 20:55–23:51 ET / 01:55–03:51 UTC, тред `spaces/AAAAn_HZbYs/threads/gNhHlan3QxI`), Anna Levadnaia (`alevadnaia@google.com`), Max Gulimonov (`maximg@google.com`).
- **Суть проблемы и дискуссии**:
  - Valeriy Leletko поднял острую проблему: с массовым распространением ИИ-генерации авторы перестали вычитывать свой код и отправляют на ревью сырые наброски с примитивными галлюцинациями, битыми ссылками на ассеты/скриншоты и непроверенными схемами. В итоге ревьюер тратит часы, чтобы по сути написать CL за автора через детальные комментарии в Critique.
  - Та же проблема наблюдается с дизайн-документами (длинные AI-галлюцинированные стены текста, авторы на 1:1 не могут ответить на вопросы по собственным дизайнам).
  - Max Gulimonov отметил, что «в конце 2026 года нужно перестать смотреть код руками, а начать настраивать жесткий тестовый и статический харнесс», а также подключать специализированных агентов ревью (readability-агенты, проверка расхождения кода со спецификацией вроде OpenSpec).
- **Вердикт полезности**: `🟡 Уже внедрено у нас`.
- **Где это реализовано у нас**:
  - У нас уже работает двусторонний защитный контур:
    1. Для исходящего кода — строгий pre-commit аудит (`amnesia-precommit`, `audit_comments.py`, `ruthless_audit.py`, `mutation_check.py`, прогон тестов фреймворка и верификатор живости тестов).
    2. Для входящих ревью — `companion-cl-review` и `review-triage`, где чужой код проверяется не доверчиво на глаз, а через контрактные тесты (`PS1` = воспроизведение дефекта, `PS2` = исправление) и верификацию скептиком (`keeper_skeptic`).

#### 2. OpenSpec: спецификационно-ориентированная разработка (SDD) и контроль дрейфа кода от спецификации
- **Авторы**: Max Gulimonov (`maximg@google.com`, 00:23 ET / 05:23 UTC, тред `spaces/AAAAn_HZbYs/threads/gNhHlan3QxI`).
- **Ссылки и ресурсы**: Документация [go/sdd](http://go/sdd), скилл [`/google/src/files/head/depot/google3/learning/gemini/agents/skills/openspec/SKILL.md`](file:///google/src/files/head/depot/google3/learning/gemini/agents/skills/openspec/SKILL.md), пакет `//third_party/javascript/node_modules/openspec`.
- **Суть решения**:
  - OpenSpec формализует 5-фазный жизненный цикл изменений через папки `{spec_root}/specs` и `{spec_root}/changes/{change_name}` (proposal -> specs -> design -> tasks -> apply).
  - Позволяет агентам сравнивать AST/схемы имплементации с утвержденными требованиями (`Requirement`) и предотвращать дрейф реализации от спецификации (Code vs Spec Drift).
- **Вердикт полезности**: `🟡 Уже внедрено у нас (в R4 и doc-check) + ⚪ Не нужно (YAGNI для C++/Skia)`.
- **Как используем**:
  - Для наших C++/Dart репозиториев (Skia, Flutter Engine) тяжелый фреймворк OpenSpec избыточен (YAGNI). Всю ценную суть (проверка дрейфа кода от проектных правил и `AGENTS.md`) мы уже зафиксировали в открытом эпике `R4` (`Epic: Doc-Check & Grounding`, задача 3: `Doc <-> Code Drift check in amnesia-precommit & review-triage`).

#### 3. Модели и олимпиадный стиль: Claude vs Gemini Fable на валидации схем и данных
- **Авторы**: Anna Nachesa (`ashalynd@google.com`, 09:31–14:26 ET / 14:31–19:26 UTC, тред `spaces/AAAAn_HZbYs/threads/u7JvixCpbyc`), Slava Guzanov (`vguzanov@google.com`), Vanya Davidenko (`vdvd@google.com`).
- **Суть дискуссии**:
  - Anna столкнулась с тем, что Claude (Opus 5.5 medium) для валидации proto-ответов предложил парсить поля через регулярные выражения, и только после прямого наводящего вопроса догадался распарсить JSON/прото напрямую.
  - Slava Guzanov указал, что самописные проверки схем регулярками — классический антипаттерн (нужно использовать Pydantic или типизированные схемы).
  - Vanya Davidenko отметил, что в слепом тестировании и на сложных задачах Gemini Fable на reasoning-уровне `high` работает заметно лучше и быстрее Опуса.
- **Вердикт полезности**: `⚪ Не нужно (YAGNI) / ознакомительно`.
  - У нас в правилах уже зафиксирован отказ от хрупких эвристик и регулярок там, где есть формальные AST/компиляторы.

---

### Chrome AI Discuss (`spaces/AAQA45T8nC8`)

#### 1. Проблемы конфигурации MCP-плагинов в `chromium-ide` на macOS (`chrome-aura-remote`, `trooper`)
- **Автор**: Adam Norberg (`norberg@google.com`, 18:14–19:09 ET / 23:14–00:09 UTC, треды `spaces/AAQA45T8nC8/threads/oJPnBssALDo`, `spaces/AAQA45T8nC8/threads/hV1OyNr6bdM`, `spaces/AAQA45T8nC8/threads/_4xSkx5LySk`).
- **Ссылки и файлы**: Конфиг [`internal/agents/plugins/chrome-aura-remote/mcp_config.json`](https://source.corp.google.com/h/chromium/chromium/src/+/main:internal/agents/plugins/chrome-aura-remote/mcp_config.json), документация [go/jetski-agent-plugins](http://go/jetski-agent-plugins).
- **Суть проблемы**:
  - В монорепозитории Chromium плагины для `chromium-ide` (`internal/agents/plugins/`) на macOS сталкиваются с багами подстановки путей: переменные `${extensionPath}` и `${/}` не раскрываются средой и трактуются буквально, вызывая `file not found`.
  - Замена на `${PLUGIN_ROOT}/` приводит к ошибкам SSH-сертификатов при попытке удаленного подключения с Mac на Cloudtop.
  - Плагин `trooper` поставляется с предкомпилированным Linux-бинарником и падает на Darwin.
- **Вердикт полезности**: `⚪ Не нужно (YAGNI) / справочно`.
  - Наша основная среда разработки — полноценный Linux Cloudtop, где все MCP-серверы и плагины запускаются нативно без удаленных SSH-туннелей и несовместимости бинарников.
