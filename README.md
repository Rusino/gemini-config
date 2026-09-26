# Gemini / Jetski Global Config (`~/.gemini/config`)

Персональные правила, хуки и настройки для Jetski (`~/.gemini/config`), совместимые с Linux и macOS.

## Что внутри

- `rules/` — глобальные правила:
  - `communication_style.md` — стиль общения («ты», женский род, критическая оценка без лести, Open Source / Git).
  - `subagent_exploration.md` — делегирование широкого поиска субагентам и проверка сигнатур API перед вызовом.
- `hooks.json` и `hooks/` — защитные хуки жизненного цикла:
  - `context_guard.py` — контроль размера контекста и автоматический перенос (handoff) в новый чат + защита от циклов ошибок (Two-Strike Debug Guard).
  - `pre_tool_guard.py` — защита от слепого редактирования без чтения (`view_file`), редактирования сгенерированных/игнорируемых файлов и сломанных команд `agentapi`.
  - `stop_guard.py` — проверка запуска сборки/тестов после редактирования исходного кода перед завершением хода.

> **Примечание:** `config.json` и `projects/` добавлены в `.gitignore`, так как они содержат привязанные к конкретной машине пути (`/usr/local/...` vs `/Users/...`), имя хоста удалённого доступа и изменяются во время работы.

## Как развернуть на Mac (или другом компьютере)

Если папка `~/.gemini/config` на Маке ещё не существует:

```bash
mkdir -p ~/.gemini
git clone git@github.com:Rusino/gemini-config.git ~/.gemini/config
```

Если папка `~/.gemini/config` уже создана при первом запуске Jetski (там уже лежит локальный `config.json`):

```bash
cd ~/.gemini/config
git init
git remote add origin git@github.com:Rusino/gemini-config.git
git fetch origin
git checkout -f main
```

## Обновление настроек

После изменений на любой из машин:

```bash
git -C ~/.gemini/config add -A
git -C ~/.gemini/config commit -m "Update config"
git -C ~/.gemini/config push
```

На другой машине:

```bash
git -C ~/.gemini/config pull --rebase
```
