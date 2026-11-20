# 2026-11-20: control file G2:0 при перепривязке реплики (этап 0b)

План: hqcluster-node `docs/switchover-keep-replicas-plan.ru.md`, вопрос В-7.
Вопрос: после перепривязки C (заголовок sequence 0, `source_guid` = G2,
mailbox очищен) node пишет control file для G2 с позицией 0. Если первым
приходит сегмент 2, ждёт ли Firebird сегмент 1? Без control file (этап 0,
r2) C молча пропускает сегмент 1.

## Ответ

**Да, на 4.0.7 и 5.0.4.** С control file G2:0 Firebird не применяет
сегменты 2…N. Каждые `apply_error_timeout` он пишет в `replication.log`:

```
ERROR: Required segment 1 is missing
VERBOSE: Disconnecting and suspending
```

Когда сегмент 1 приходит, Firebird применяет 1, 2, … и C = B. Шаг 9 §3
плана нужен: control file G2:0 даёт защиту на уровне Firebird, а не только
node.

Без control file (c0) Firebird сам создаёт файл с позицией «первый
найденный сегмент − 1», применяет 2…, а сегмент 1 потом удаляет:

```
VERBOSE: Deleting segment 1 as no longer needed
```

C расходится с B без ошибки (на 4.0 видно ещё `WARNING: Record being
updated in table T does not exist, inserting instead`).

Причина в коде (`src/remote/server/ReplServer.cpp`, класс `ControlFile`,
v4.0-release и v5.0-release): позицию «первый найденный − 1» Firebird
берёт, только если файл **пустой** (`length == 0`). Если файл есть,
`last_sequence` = 0, следующий нужный сегмент = 1, а сегмент больше
нужного даёт «Required segment … is missing».

## Стенд

- Два временных LXD-контейнера (Ubuntu 24.04), ванильный Firebird
  4.0.7.3271 и 5.0.4.1812, SuperServer (`firebirdlinuxinstall/
  fb_vanilla-40.sh`, `fb_vanilla-50.sh`). После прогона контейнеры удалены.
- Как на этапе 0: три базы на одном сервере (A мастер, B и C реплики),
  сегменты копирует сам скрипт. Переключение A→B, promote B (новый GUID G2),
  перепривязка C: `gfix -replica none`, `nbackup -L`, `nbackup -F` без
  `-SEQUENCE`, `gfix -replica read_only`, очистка mailbox, control file,
  `source_guid` = G2, рестарт Firebird.
- Control file пишет скрипт (python3) в формате node
  (`fbheader.WriteReplicaControl`): 40 байт, little endian,
  `"FBREPLCTL\0"`, version 1, txn_count 0, **sequence 0, offset 0,
  db_sequence 0** (= `Replication sequence` C после `-F`). Имя —
  `{G2}` в верхнем регистре, в `journal_source_directory` C, владелец
  firebird, режим 0660.

## Запуск

```sh
FB_PASSWORD=... ./2026-11-20-replica-control-seed.sh c1   # или c2, c0
```

Скрипт стирает `/data` и переписывает `/opt/firebird/replication.conf`:
только на чистом временном хосте. Код выхода 3: подготовка попала на
зависание fd 0 (ниже), прогон не годится, его повторяют.

- **c1:** control file G2:0; сегмент 1 B задержан, 2… отправлены; через
  25 с сегмент 1 отправлен.
- **c2:** control file G2:0; обычная отправка (нет регрессии).
- **c0:** как c1, но без control file (случай r2 этапа 0, для сравнения).

## Результат

Годные прогоны (без зависания при подготовке):

| Сценарий | 4.0.7 | 5.0.4 |
|---|---|---|
| c1, G2:0, сегмент 1 позже | 3 из 3: C ждёт, потом C = B | 4 из 4: C ждёт, потом C = B |
| c2, G2:0, обычно | 1 из 1: C = B | 2 из 2: C = B |
| c0, без control file | 1 из 1: C ≠ B, сегмент 1 удалён | 1 из 1: C ≠ B, сегмент 1 удалён |
| Зависание fd 0 при подготовке | 5 из 10 прогонов | 1 из 8 прогонов |

- **c1:** пока сегмент 1 задержан, данные C не меняются (digest C =
  digest до переключения), в mailbox лежат сегменты 2…6 и control file с
  sequence 0. После сегмента 1 C = B через 2–4 с; ещё одна порция
  нагрузки — тоже C = B. Control file в конце: sequence 10, db_sequence 0.
- Заголовок C всё время: `Replication sequence: 0`. Firebird его не
  меняет (F-2 плана).

Строки `replication.log` C в c1 (4.0 и 5.0 одинаково), раз в 3 с:

```
VERBOSE: Added 5 segment(s) to the queue
ERROR: Required segment 1 is missing
VERBOSE: Disconnecting and suspending
```

## Находка: реплика зависает, если control file открыт как fd 0

Не относится к В-7, но может остановить любую реплику без строки в логе.

- **Где:** `src/jrd/replication/Utils.h`, класс `AutoFile`, `release()`:
  `if (m_handle) { ::close(m_handle); ... }`. Дескриптор 0 считается
  «нет файла» и не закрывается. Одинаково в v4.0-release, v5.0-release и
  master. `ControlFile` в `ReplServer.cpp` наследует `AutoFile` и берёт
  `flock(LOCK_EX)`.
- **Почему fd 0 свободен:** SuperServer при старте закрывает все
  дескрипторы, кроме 1 и 2 (`inet_server.cpp` → `divorce_terminal`).
  Потом fd 0 занимают и освобождают разные файлы; иногда его получает
  control file.
- **Что происходит:** control file на fd 0 не закрывается, его flock
  остаётся. Следующее открытие этого control file (следующий сегмент того
  же GUID) ждёт в `flock()` всегда. Реплика не применяет сегменты, в
  `replication.log` для неё нет ни одной строки, другие реплики работают.
- **Как увидеть:** `ls -l /proc/<pid firebird>/fd/0` указывает на
  `.../{GUID}` в mailbox; в `/proc/<pid>/fd` два дескриптора на этот файл;
  поток ждёт в `flock` (gdb) или `locks_lock_inode_wait`
  (`/proc/<pid>/task/*/wchan`). Скрипт проверяет это (`fd0`, `hung`) и
  выходит с кодом 3.
- **Как повторить:** этот скрипт, любой сценарий; на 4.0 зависание
  случилось в половине прогонов, при подготовке (12 сегментов приходят
  сразу, «preparing for replication reset»).
- Отдельно: 3 из 12 рестартов после перепривязки шли ~100 с вместо ~3 с
  (`systemctl` ждал 90 с). Причина не проверена.

## Открыто

- Сообщить о `AutoFile` в Firebird (исправление: `m_handle >= 0`,
  «нет файла» = -1).
- HQbird не проверен.
