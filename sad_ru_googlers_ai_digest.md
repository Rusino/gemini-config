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
- **Связь с нашими правилами**: Полностью подтверждает наше правило в `~/.gemini/config/rules/code_style.md` не публиковать драфты и ответы автоматически без явного аппрува пользователя, а также требует осторожности при генерации черновиков через `git cl comment`.

#### 2. Upskill: Telemetry-Driven Surgical Skill Upgrader (go/jetski-upskill, go/100-skills)
- **Авторы**: Narender Gaddipati (`narenderg@google.com`), репост: Javier Castro (30 окт. 19:55 ET) и Joe Mason (2 нояб. 11:14 ET, тред `spaces/AAQA45T8nC8/threads/rZMXgxljhco`, оригинал `spaces/AAQAN-vNGWg/threads/GE2cjsaIJI4`).
- **Путь в репозитории**: [`/google/src/files/head/depot/google3/prototypes/projects/upskill/skills/upskill/SKILL.md`](file:///google/src/files/head/depot/google3/prototypes/projects/upskill/skills/upskill/SKILL.md).
- **Ключевые инварианты и практики**:
  1. *Оптимизация топ-100 скиллов Google3*: ресерч покрыл 97.6% трафика скиллов в google3, вернув ~650 млрд токенов в месяц за счёт устранения лишних повторных перечитываний (`re-read friction`).
  2. *Статический доктор из 18 проверок (`--skip-f1`)*: лимит `view_file` ровно 800 строк или 46 080 байт (45 KB); запрет выносить заглушки без флагов в `## Quick Start` (провоцируют агентов спамить `--help`); запрет удалять флаги при сжатии документации; правило 0-tool halt для защитных инструкций.
  3. *Zero Content Loss (`verify-diff`)*: валидация того, что ни один флаг, ссылка или параметр не потерялись при рефакторинге скилла.
  4. *Бенчмаркинг Evalin*: прогон тестов `EVAL.txtpb` с обязательным контролем отсутствия регрессий (`Candidate Pass Rate >= Baseline Pass Rate`).

#### 3. Herdr и CRD `terminal_mode` для многоагентной работы в CLI
- **Авторы**: Judith Hemp (`hempjudith@google.com`), Arthur Sonzogni, Ned Nguyen (30 окт., тред `spaces/AAQA45T8nC8/threads/LWYatSLRkx0`), Joe Downing (тред `spaces/AAQA45T8nC8/threads/LZ2CdRU_w1Q`).
- **Суть**:
  - `herdr.dev`: консольный менеджер для многоагентных сессий на Cloudtop, альтернатива tmux/shpool с уведомлениями и интеграцией с ворктри.
  - Эксперимент Chrome Remote Desktop `terminal_mode` (`https://remotedesktop.corp.google.com/access/?mods=terminal_mode`) для прямого управления терминалами агентов из браузера.
