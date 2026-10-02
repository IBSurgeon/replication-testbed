# Смена GUID при promote и связь мастер–реплика: прогон 2026-10-01

Итог: **promote со сменой GUID на месте работает через работающий сервис HQbird под
нагрузкой** на 4.0 и 5.0. На 2.5 и 3.0 он работает на копии реплики. Все сбои — ошибки
логики тестов, исправлены. Признак реплики, пересозданной без `-SEQUENCE`, на 4.0 есть.
Сейчас узел его не видит.

Вопросы и план: hqcluster-node `docs/replica-own-guid-plan.md` (раздел 10),
hqcluster3 `docs/RCM_UI_DB_TABLE_MERGE_PLAN.md` (§11.1).

## 1. Стенды

| Стенд | Firebird | Хосты | Тесты |
|---|---|---|---|
| демо (m3) | HQbird 5.0 | master, replica1 (+ companion), replica2 | `guidpromote` |
| g40 | HQbird 4.0.8 | master, replica1 (+ companion), replica2 | `guidprobe`, `noseq`, `guidpromote` |
| g30 | HQbird 3.0.15 | master, replica1 | `guidprobe` |
| g25 | HQbird 2.5.9 | master, replica1 | `guidprobe` |

Сборка узла и RCM — 2027.4.2.1. Адреса и учётные данные — только в локальных конфигах.

## 2. Новое в стенде

- `hostctl`: `db-header`, `db-copy-locked`, `guid-promote`, `segment-guids`, `replace-db`
  (`docs/modules.md`).
- Тесты `guidpromote`, `guidprobe`, `noseq` (`docs/tests.md`).

`guid-promote` делает шаги через работающий Firebird:

1. `gfix -replica none` (2.5/3.0: `{}`);
2. `gfix -shut single -force 0`;
3. `nbackup -L` и `nbackup -F` без `-SEQUENCE` по `localhost/порт:путь`;
4. удалить delta;
5. `gfix -v -full`;
6. `gfix -online`.

Заголовок читается до, после `-F` и в конце.

## 3. Результаты

| Тест | Стенд | Итог | Упало |
|---|---|---|---|
| guidpromote | 5.0 | 21 из 23 | номер после `online` = 1 (ждали 0); TB_PROBE на replica2 |
| guidpromote | 4.0 | 22 из 23 | TB_PROBE на replica2 |
| guidprobe | 4.0 | 17 из 17 | — |
| guidprobe | 3.0 | 12 из 12 | — |
| guidprobe | 2.5 | 12 из 12 | — |
| noseq | 4.0 | 9 из 10 | «узел видит» — ожидаемый FAIL до `replica_sequence_reset` (4.4) |

Оба сбоя `guidpromote` — логика теста:

- **Номер 0**: публикующий мастер сразу после `gfix -online` открывает сегмент 1. Тест
  теперь проверяет номер после `-F` (на 4.0 уже так: «32 → 0 after -F, 1 online»).
- **TB_PROBE**: таблицу создаёт `write-probe` на новом мастере уже после sync publication.
  Узел публикует таблицы, которые были при sync (`EXCLUDE ALL`, затем
  `ENABLE PUBLICATION` по таблице). Строки новой таблицы не реплицируются до следующего
  sync — так на любом мастере. Сравнение строк теперь пропускает TB_PROBE.

## 4. Ответы

### 4.1 Promote через работающий сервис под нагрузкой (4.0, 5.0)

- RCM promote ok. Повышенная база имеет GUID старого мастера, RCM поднимает
  `duplicate_guid`.
- Все шаги `guid-promote` rc 0. Каждый шаг занимает 0–1 с, база под нагрузкой.
- GUID новый, номер 0 после `-F`, `gfix -v -full` чисто. Delta была и удалена. PID
  Firebird не изменился.
- Сегменты нового мастера — 1 и 2 с новым GUID. Чужих GUID в каталогах журнала нет.
- `duplicate_guid` уходит. В блоке нового мастера нет реплик старого (нет S1).
- Initialize нового мастера на replica2 проходит. Реплика догоняет 2 минуты нагрузки:
  строки равны, кроме TB_PROBE.
- Initialize старого мастера на повышенный файл — 409 на precheck. GUID и запись в базу
  сохранены.
- Сходимость: старый мастер → replica2, другие базы на replica1 — строки равны.
- Companion поднимает `db_file_replaced`: тест меняет GUID после enroll. Шаг `new_guid`
  в RCM будет до enroll.

### 4.2 HQbird 2.5/3.0

- Reinit узла даёт реплике GUID мастера. `Replication master GUID` = GUID мастера.
- Promote без смены GUID публиковал бы под GUID мастера. Смена GUID нужна.
- `gfix -replica {}` + `-L`/`-F` даёт новый GUID и номер 0, очищает
  `Replication master GUID`. Firebird не перезапускается.

### 4.3 RCM и пары 2.5/3.0

Пары сопоставляются: GUID файла реплики = GUID мастера, группа держит реплику, «Ok».
RCM показывает GUID в канонической форме. gstat 2.5/3.0 печатает его в другом порядке
слов: `F0EA5E19-1555-4412-80AE-…` против `5E19F0EA-1555-4412-AE80-…`.

### 4.4 4.0: реплика без `-SEQUENCE` поверх работавшей

Три прогона, числа последнего:

- **Исходное состояние:** reinit с `-SEQUENCE`. Номер реплики 22, control file:
  `db_sequence` 22, `sequence` 27.
- **После пересоздания по руководству** (`-F` без `-SEQ`, своя GUID, read_only):
  номер 0, control file тот же (`db_sequence` 22). **Признак есть.**
- **Следующие 90 с нагрузки:**
  - `sequence` 27 → 36, `db_sequence` остаётся 22 — сегменты уходят без применения;
  - строк на реплике нет;
  - ошибок в `replication.log` нет. Узел ставит `verbose_logging = true`, поэтому есть
    строки VERBOSE. На каждый сегмент — пара: «Database sequence has been changed to 0,
    preparing for replication reset» и «Segment 33 … is scanned …, deleting» вместо
    «is replicated».
- **Узел и RCM:** состояние IN_SYNC, RCM «Ok». logwatch разбирает только «is
  replicated». Есть только алерт `db_file_replaced` (serious) — сменился GUID файла. Его
  нет, если узел не видел старый файл (например, был перезапущен).

Первая версия теста засчитывала `db_file_replaced` как обнаружение и не находила строки
лога: путь базы стоит в соседней строке записи. Теперь тест требует состояния или
статуса RCM (до реализации `replica_sequence_reset` — FAIL), читает лог по записям и
проверяет, что признак держится, пока сегменты уходят.

## 5. Стенды после прогона

Демо: db9 на replica1 — мастер companion с новым GUID, его реплика — на replica2. Нагрузка
пользователя (`load`, все базы, по 1 соединению) запущена снова. g40: db3 на replica1
повышена так же. g25, g30, g40 работают — удалить, когда не нужны.
