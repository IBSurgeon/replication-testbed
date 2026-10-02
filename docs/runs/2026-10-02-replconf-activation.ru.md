# Активация replconf на HQbird 2.5/3.0 без ручных chmod: прогон 2026-10-02

Итог: **правки fbagent работают** на HQbird 2.5.9, 3.0.15 и 5.0.5. Задача —
hqcluster-node `docs/fbagent-replconf-activation-task.md`, разд. 3.7.
Прогнаны S1, S2 (вручную через API агента), S3 (частично), S4–S10. Узел
не менялся (2027.4.2 из `stable`), поэтому S2/S3/S8/S9 проверены без
автоактивации узла. Найдено одно замечание к узлу (разд. 5).

## 1. Стенд

| Что | Значение |
|---|---|
| Хосты | 6 дроплетов DO 4 ГБ, Ubuntu 22.04, lon1: пары master/replica на HQbird 3.0.15, 2.5.9, 5.0.5 |
| goafts | chess1 (лаборатория) |
| Кластеры | `act30`, `act25`, `act50`: master → replica, RCM на мастере, окна `always`; у 2.5/3.0 `replconf_valid_till` 2027-06-30 |
| Установка | fbagent `ops/linux-install/fbagent-fb{30,25,50}_known.sh --goafts chess1 --enroll --cluster` |
| Узел, RCM | 2027.4.2 (`stable` chess1), ставит агент |
| Адреса | только в локальных файлах оператора |

## 2. Сборка

fbagent **2.56.1-lab.1** в канале `ops-test` chess1: `fee7475` + незакоммиченные
правки задачи (база `clusterprov`/`nodecontract` и п. 2.2–2.4). Чужая строка в
`internal/releasesig/vendor_keys.pub` в сборку не вошла. Сборки без подписи.

| Файл | SHA-256 |
|---|---|
| fbagent-linux-amd64 | `92b8a551…c8445f8` |
| goafts_enroll-linux-amd64 | `88c7477d…a2a38be` |

Плагин для S2 — `linux-x86_64/libreplconf.so` replconf 2.1.0 из поставки узла
(`6f750865…ef86bd8f`, закреплён в fbagent).

## 3. Результаты

| # | Сценарий | 3.0.15 | 2.5.9 | 5.0.5 |
|---|---|---|---|---|
| S1 | Установка с нуля сразу новой сборкой (`ops-test` с первого шага) | да | — | да |
| S2 | Активация через `POST /v1/instances/{id}/replconf/install` + restart | да | да | — |
| S3 | Репликация: reinit, запись на мастере → чтение на реплике | да | да | да |
| S4 | Обновление с 2.55.3 | — | да | — |
| S5 | Хост с закрытым `<root>` (П1) чинится обновлением агента | — | да | — |
| S6 | Атаки от `firebird` через `sudo -n` на живом хосте | да | — | — |
| S7 | Повторная активация | да | — | — |
| S8 | Плагин заменён при работающем Firebird | да | да | — |
| S9 | Откат по копии `.hqcluster-<время>` | да | — | — |
| S10 | FB5: `replication.conf` как раньше, маршрут → `not_replconf_engine` | — | — | да |

Подробно:

- **S1.** Узел и RCM поставлены агентом. Агент открыл корень для файла узла
  (журнал: `HQCluster node install into …: chgrp firebird /opt/firebird; chmod
  g+w,+t /opt/firebird`, аудит `hqclusternode_prepared`). Корень
  `root:firebird 1775`. В `node.json` `replication_conf` пустой, дата
  2027-06-30. Узел создал `/opt/firebird/replconf.hqcluster.hqbird`. На FB5
  `firebird-conf` отдал узлу `replication.conf`, как раньше.
- **S2.** `GET /v1/instances/{id}` → `capabilities: ["replconf_install"]`.
  `check: true` → `check: ok`, на диске ничего. Установка → `plugin:
  installed`, `properties: written`. После: `plugins/`, `bin/` —
  `root:root 755`; плагин `root:root 644`, хеш закреплённый;
  `replconf.properties` — `/opt/firebird/replconf.hqcluster.hqbird\r\n`;
  временных файлов нет; в журнале агента по строке на вызов (хеши до/после,
  копии). Один перезапуск Firebird через `POST …/restart`. Плагин загружен в
  процесс Firebird (`/proc/<pid>/maps`). Узел: `active: true`,
  `plugin_version: 2.1.0`, `valid_till: 2027-06-30`. На 2.5 то же (движок
  узнан по файлам: версия у агента `Unknown`).
- **S3.** База на мастере, `scansync`, на 2.5/3.0 перезапуск Firebird мастера,
  на FB5 `publication/sync`, `reinit --mode standard` на реплику, вставка на
  мастере — строка на реплике на всех трёх движках. Критерии даты задачи о
  сроке не прогонялись (нужна правка узла).
- **S4.** 2.5-пара поставлена `stable` (2.55.3), затем
  `goafts.auto_update.channel = ops-test` и рестарт агента. Агент обновился;
  помощник в `libexec` новый (`replconf-install` есть, `root:root 755`).
  `node.json`: путь `replication.conf`, записанный старым агентом, убран, дата
  записана.
- **S5.** Перед обновлением корень закрыт вручную (`root:root 755`, как после
  сборки с П1). Узел после смены `node.json` не смог создать файл
  (`open /opt/firebird/replconf.hqcluster.hqbird.lock: permission denied`).
  Новый агент на первой проверке ещё видел старый `node.json`, на следующей
  (через 3 мин) открыл корень: `HQCluster node in …: chgrp firebird
  /opt/firebird; chmod g+w,+t /opt/firebird` — без выпуска узла. Узлы
  создали файл сами: реплика через 1 мин, мастер через 10 мин (разд. 5).
- **S6.** От `firebird`, `sudo -n launch-install-hqclusternode.sh …`: поддельная
  `.so` → `plugin_not_pinned`; `conf=/etc/passwd` → `not a replconf file
  path`; `root=/etc` → `not a Firebird folder`; conf — ссылка на
  `/etc/shadow` → отказ; `firebird-conf` на `firebird.conf` → отказ; плагин —
  ссылка на закреплённую копию → отказ. Снимок корня, `plugins/`, `bin/`
  (владельцы, режимы, inode, списки) до и после — равен.
- **S7.** Второй вызов → `plugin: unchanged`, `properties: unchanged`; mtime и
  inode целей прежние, PID Firebird прежний, копий нет.
- **S8.** Плагин поставлен при работающем Firebird: до перезапуска новых
  ошибок в `firebird.log` нет, attach работает; после — узел видит 2.1.0,
  `active: true`.
- **S9.** На мастере 3.0 `replconf.properties` вручную направлен на
  «старый» файл, повторная установка → `properties: written
  backup=…/replconf.properties.hqcluster-20261002T111324Z` (в копии — старое
  содержимое). Откат: копия на место, перезапуск — Firebird работает, данные
  читаются, узел сообщает, что движок читает не его файл. Копия плагина на
  стенде не создаётся (закреплена одна сборка); она проверена root-тестами
  fbagent.
- **S10.** FB5: `firebird-conf` с `replication.conf`, маршрут → `409
  not_replconf_engine`, на диске ничего; репликация FB5 работает (S3).

## 4. Падение Firebird 3.0 на реплике (отчёт 2026-10-01, разд. 6 п. 3)

**Повторилось.** Реплика 3.0, через ~1 мин после первого сегмента:
`pthread_mutex_unlock failed. Error code 1`, затем `firebird terminated
abnormally`, fbguard поднял сервер, данные дошли. fbagent с задачами трасс
по умолчанию. На 2.5 и FB5 сбоев нет. Провалом этой задачи не считается.

## 5. Замечание к узлу (2027.4.2)

Если при старте узла корень Firebird закрыт, `startup_scansync` падает
(`permission denied`), и файл replconf появляется только на следующем
плановом проходе узла: на реплике через 1 мин после открытия корня, на
мастере через 10 мин. До этого вызов активации отказывает (`helper_refused`:
файла узла нет). Для задачи узла: при отказе прохода повторять раньше или
создавать файл перед активацией.

## 6. Не сделано

- Сценарии не оформлены как тесты test bed (`tests/*.py`): прогон ручной,
  команды — у оператора. Автоматизация — вместе с задачей узла (S2/S3/S8/S9
  целиком).
- Критерии даты (S3) — после правки узла.

## 7. Стенд после прогона

Удалено: 6 дроплетов, кластеры `act30`/`act25`/`act50` и 6 агентов на
chess1. В `ops-test` chess1 остаётся fbagent 2.56.1-lab.1 (лабораторный
канал).
