# Church Skills

[![Validate skills](https://github.com/gtp-church/ai-skills/actions/workflows/validate.yml/badge.svg)](https://github.com/gtp-church/ai-skills/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Скиллы для церковного служения от [gtp-church](https://github.com/gtp-church).
Используют открытый стандарт [Agent Skills](https://agentskills.io) и подходят для
совместимых AI-ассистентов, включая Claude Code и Codex.

## Что есть

| Скилл | Назначение |
| --- | --- |
| [Sermon Deck](skills/sermon-deck/SKILL.md) | Презентация к проповеди из PDF: чёрный фон, крупный текст, стихи Писания, полный конспект в заметках докладчика. PPTX и PDF для показа. |

Шаблон, логотип и метрики шрифтов входят в пакет. Папка с прошлыми презентациями необязательна.

## Установка за минуту

Понадобятся [Node.js LTS](https://nodejs.org/) (включает `npx`) и ассистент с поддержкой скиллов.
Выполните в терминале:

```bash
npx skills add gtp-church/ai-skills --global
```

CLI определит установленных агентов и при необходимости предложит выбрать
скиллы и получателей. Выберите нужных агентов и способ установки **Symlink**:
скилл хранится в общей папке, а агенты получают ссылки на него. Откройте новую
сессию ассистента — скилл будет доступен во всех проектах на этой машине.

Claude Code поддерживает такую установку: ссылка создаётся в `~/.claude/skills/`.
Одной общей папки без ссылки для Claude Code недостаточно.
См. [документацию Claude Code](https://code.claude.com/docs/en/skills#where-skills-live).

Чтобы выбрать конкретный скилл сразу:

```bash
npx skills add gtp-church/ai-skills --skill sermon-deck --global
```

Для установки только в текущий проект выполните команду в его папке без `--global`.
Если команду запускает сам AI-ассистент, CLI может автоматически выбрать этого
ассистента; для установки сразу нескольким агентам запустите команду вручную
в обычном терминале или явно укажите получателей через `--agent`.

Проверка и обновление:

```bash
npx skills list --global
npx skills update sermon-deck --global
```

Синтаксис и дополнительные варианты: [документация npx skills](https://github.com/vercel-labs/skills#install-a-skill).
Глобальная установка действует на этой машине; команда не загружает скиллы в веб-чат Claude или Cowork.

## Как пользоваться

Положите PDF-конспект в рабочую папку и напишите ассистенту:

> Используй sermon-deck. Сделай презентацию к проповеди по файлу «Конспект.pdf».
> Сохрани PPTX и PDF рядом с конспектом и проверь, что весь текст попал в заметки докладчика.

В Codex можно указать `$sermon-deck` явно. В Claude Code — `/sermon-deck` или название в запросе.
Ассистент прочитает конспект, соберёт слайды, проверит их и подготовит PDF для резервного показа.
Текст слайдов и стихи перед служением стоит просмотреть.

## Программы для создания презентаций

Установка скилла добавляет инструкции и файлы; системные программы ставятся отдельно.

| Программа | Для чего нужна |
| --- | --- |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Запускает Python-скрипты и автоматически устанавливает их Python-зависимости. |
| [Poppler](https://poppler.freedesktop.org/) | Чтение PDF (`pdftohtml`, `pdftotext`) и картинки для проверки (`pdftoppm`). |
| [LibreOffice](https://www.libreoffice.org/download/download-libreoffice/) | Преобразование PPTX в PDF. |

**macOS с Homebrew:**

```bash
brew install uv poppler
brew install --cask libreoffice
```

**Ubuntu / Debian:**

```bash
sudo apt-get install poppler-utils libreoffice-impress
```

`uv` установите по ссылке выше. В Windows установите uv, Poppler и LibreOffice и добавьте
исполняемые файлы в `PATH`. Если LibreOffice установлен в нестандартном месте, задайте
`SOFFICE` — полный путь к `soffice` / `soffice.exe`.

Для сборки PPTX достаточно Python 3.10+ и `python-pptx`; Poppler и LibreOffice нужны
на этапах чтения конспекта и подготовки PDF. Метрики шрифтов уже в пакете,
встраивание шрифтов в PPTX зависит также от поддержки программы просмотра;
для одинакового показа используйте проверенный PDF.

## Разработка и новые скиллы

Каждый скилл находится в `skills/<name>/` и содержит `SKILL.md` с полями `name` и `description`.
Скрипты, справочные материалы и шаблоны лежат рядом. См. [CONTRIBUTING.md](CONTRIBUTING.md).
Ошибки и предложения — в [Issues](https://github.com/gtp-church/ai-skills/issues).

## Лицензия

Исходный код и инструкции — [MIT](LICENSE). Шрифты сторонних авторов,
встроенные в шаблон, не перелицензируются; см. [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
