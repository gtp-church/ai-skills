# Как внести вклад

Опишите проблему или предложите новый скилл в Issue. Для изменений создайте ветку
в своём форке и откройте Pull Request с описанием результата и проверок.

- Имя папки и `name` в `SKILL.md` совпадают: строчные латинские буквы, цифры и дефисы.
- Отображаемое имя (`display_name` и заголовок `SKILL.md`) — на английском в Title Case, например `Sermon Deck`.
- `description` кратко объясняет назначение и случаи применения.
- Пути к ресурсам относительны папке скилла. Не добавляйте пути конкретного пользователя.
- Сохраняйте исходный смысл конспекта, точность цитат и полноту заметок докладчика.
- Не коммитьте личные конспекты, результаты генерации, секреты, кэши и ресурсы без прав на публикацию.
- Для новых файлов сторонних авторов укажите источник и лицензию.

## Проверка

```bash
uv run --with python-pptx --with pyyaml python -m unittest discover -s tests -v
uv run --with python-pptx python -m unittest discover -s skills/sermon-deck/tests -v
python3 -m unittest discover -s skills/mcy-music-find-new-songs/tests -v
python3 -m unittest discover -s skills/mcy-music-team-announce/tests -v
```

Пример сборки и визуальная проверка (из корня репозитория):

```bash
uv run skills/sermon-deck/references/example_build.py --output /tmp/sermon-example.pptx
uv run skills/sermon-deck/scripts/check.py /tmp/sermon-example.pptx /tmp/sermon-render
```

Посмотрите полученные PNG, особенно плотные списки и длинные заголовки.
Проверки CI подтверждают структуру пакета, сборку, заметки и переносимость поиска LibreOffice;
они не заменяют просмотр в PowerPoint и визуальную проверку.
