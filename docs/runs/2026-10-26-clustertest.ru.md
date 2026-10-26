# Ревизия 2026-10-22: прогон 2 — кластер clustertest на CC2, серия 2027.4.9 → 2027.4.10 → 2027.5.0 (2026-10-23 / 2026-10-26)

Итог: **путь обновления через fbagent из канала `stable` пройден** (6 узлов + RCM,
три шага за 1.5–2.5 мин каждый, состояния баз не менялись, службы не падали);
**сценарии кольца** (нагрузка, promote по кольцу с новым GUID, reinit, kill узлов,
рестарт RCM посреди reinit, перенос реплики, час офлайна, RCM UI и CSRF) **прошли**,
кроме двух: `reinit --all` под нагрузкой оставляет пару в паузе (Ф-10) и мастер с
единственным недоступным peer час не поднимает ни одного алерта (Ф-11). **Внимание:**
с 10:57 UTC узлы работали на незапланированно опубликованной в `stable` 2027.5.1
из origin/main (без правок этапов 0–3); RCM 5.1 не стартовал на state.json схемы 2,
агент откатил его на 5.0 (раздел 2, Ф-15). Побочно: движок HQbird 5.0.5 падал на
всех трёх хостах под профилем `write-heavy` fb-loadgen (раздел 4). Найдено 15
замечаний к узлу/RCM (раздел 10), серьёзных — 3.

План: hqcluster-node `docs/review-2026-10-22-plan.md`, «Проверка на стендах», п. 2.
Прогон 1 (публичный test bed на лабе) — `2026-10-23-review-stages.ru.md`.

## 1. Стенд и выпуски

| Что | Значение |
|---|---|
| Хосты | LXD-контейнеры `clustertest1..3` на CC2 (Ubuntu 24.04, 2 CPU / 2 GiB), на каждом узел-мастер `ctNb-master` (:7051) и узел-реплика `ctNb-replica` (:7052); RCM `cc2-rcm` на самом хосте CC2 |
| Движок | Firebird 5.0.5 HQbird, один экземпляр на хост (обслуживает и мастер-, и реплика-базы) |
| Агент | fbagent 2.74.x (хост CC2 — 2.74.2), канал `stable`, проверка выпусков каждые 180 с, узлы и RCM ставятся/обновляются агентом; при неудачном health check после обновления агент откатывает продукт (п. 2) |
| Выпуски | узел `v2027.4.9` (3fbecca, минор 14), `v2027.4.10` (5e61eba, минор 15), `v2027.5.0` (44164c6, сборка 4377f2f, минор 16, схема состояния 2004); RCM `v2027.4.9` (87f2189), `v2027.4.10` (a8bde66), `v2027.5.0` (84cb182, сборка e3ae862, минор 16, state.json схема 2). Теги в origin, main не трогался (решение владельца: лицензионная пауза с origin/main вливается после прогона как минор 17). В ходе прогона в `stable` появились `v2027.5.1` обоих продуктов с origin/main — п. 2 |
| Публикация | `cc2publish --instance cc2 --products hqbirdrcm,hqclusternode --require-signature`; cc2 принимает только подписанные выпуски (ключ выпуска зоны EU, key id `28bd0766eba07e04`) — первая попытка без подписи отвергнута сервером |
| Кольцо (после пересборки) | test1: ct1b-master → ct2b-replica; test2: ct2b-master → ct3b-replica; test3: ct3b-master → ct1b-replica; test4 (добавлена в п. 5): ct1b-master → ct2b-replica |

Секреты (пароль оператора RCM, SYSDBA контейнеров) — только в локальном конфиге
`config/testbed.ct.local.json`; пароль оператора RCM на cc2 переустановлен в этом прогоне
(users.json, HA1), SYSDBA контейнеров ротированы в конце. Скрипты прогона — локально,
`state/ct-20261023/tools/` (в репозиторий не входят).

## 2. Путь обновления (2026-10-23, 19:53–20:02 UTC)

Кольцо до прогона: 2027.4.8 (минор 13) на всех узлах и RCM, с остатками опытов
октября (ORPHANED test1, вложенные копии после promote, корень ct1b-master
перенесён в `/databases/replica`). Обновлялось как есть — это и есть путь клиента.

| Шаг | Все 6 узлов + RCM | Состояния баз (state.json) | Журналы |
|---|---|---|---|
| 4.8 → 4.9 | 2 мин; агент: «vendor signature verified (key 28bd…)», стоп/старт каждой службы, старые `.bak` подчищены | одна строка `CONFIGURED → IN_SYNC` (вложенная копия на ct2b-master; остальные 9 без изменений) | 4.9–13.8 МБ → ~1 КБ (сжатие при старте, `.bak` сохранён) |
| 4.9 → 4.10 | 1.5 мин | без изменений | — |
| 4.10 → 5.0 | 2.5 мин | без изменений; узел схема 2004, RCM state.json 2 | — |

Ни одного rollback/revert, все службы `active`; после 5.0 локальные команды узла
(`status`, `listdatabases`) работают. Замечание: после 5.0 на ct1 поднялся
`node_permissions_failed` (Ф-1).

**Незапланированный шаг 5.0 → 5.1 (2026-10-26).** Пока шли сценарии, в `stable`
были опубликованы `hqclusternode v2027.5.1` (52ca953) и `hqbirdrcm v2027.5.1`
(3f097af) — теги с origin/main (лицензионная пауза, протокол минор 14), **без
правок этапов 0–3**. Агент обновил все шесть узлов в 10:57–11:01 UTC (подпись
проверена, службы перезапущены, состояния баз не изменились). RCM 5.1 в
11:13:14 UTC не стартовал: «state.json has state_schema_version 2, which is
newer than this build supports (1)», шесть рестартов systemd за 30 с, после чего
агент **откатил RCM на 2027.5.0** («health check timed out after 30s») и
заблокировал 5.1 (`blocked-versions.json`); откат занял 32 с, кольцо не
пострадало. Для чтения результатов: п. 3–5 (пересборка, нагрузка, promote по
кольцу) шли на узле 2027.5.0; п. 6–9 (reinit, аварии, час офлайна, UI) — на узле
2027.5.1 из main; RCM всё время 2027.5.0 (Ф-15).

## 3. Пересборка кольца (2026-10-26, 09:27–09:31 UTC)

Через операторский API RCM (`PUT /v1/nodes/{id}/config`, `scansync`,
`DELETE /v1/nodes/{id}/databases/{db_id}`, `ghostreplica/clear`, `firebird/restart`,
`reinit`): корень ct1b-master возвращён в `/databases/master` (test1 вернулась из
ORPHANED в IN_SYNC с прежней репликой, gen 2), пять лишних записей забыты, файлы
удалены, два Ghost-надгробия сняты, test3 заново инициализирована на ct1b-replica.
Итог — три пары IN_SYNC, алертов нет. Попутно: Ф-2 (Ghost после reinit), «последняя
база региона» (Ф-3, встретилась позже).

## 4. Нагрузка и проверка данных

- **fb-loadgen `write-heavy` не подходит:** он выедает строки `CUSTOMER`/`EMPLOYEE`
  базы employee и не стартует повторно; его сценарии с limbo-транзакциями
  **уронили движок на всех трёх хостах** (`firebird.log`: «Failure working with
  transactions list: transaction to unlink is missing in the attachment», затем
  «fbguard: firebird terminated abnormally (-1)»; ct1 09:33:41, ct3 09:34:57, ct2
  09:35:18; на ct2 ещё «pthread_mutex_lock failed. Error code 22»). На ct3 после
  перезапуска движок завис (DDL и запросы к `mon$` не возвращались), снят только
  `firebird/restart` через RCM («killed (did not terminate)»). На хостах работала
  trace-сессия fbagent (`fbtracemgr`). Узлы подняли `firebird_log_error` (D-03) и
  `replication_sequence_reset`; репликация восстановилась сама. Это дефект
  движка/окружения, не узла — отдельный репорт.
- Нагрузка прогона: свой загрузчик `lg-simple` (isql, пакеты по 150–200 insert +
  update в таблицу `LOADTEST`, 0.3 с пауза) как transient-юнит systemd на каждом
  мастере; таблица создана после публикации — `setpublication` обязателен (E-05:
  иначе строки не реплицируются, замечено по пустой таблице на реплике).
- Проверка: `POST /v1/verify` RCM по трём парам — `ok` (сравнивает только
  настроенные sync-таблицы, по умолчанию `RDB$FIELDS`); дополнительно счёт строк
  `LOADTEST` isql на мастере и реплике после остановки нагрузки — **равны** на всех
  парах (14800/14800, 14800/14800, 12200/12200), позже после promote и reinit —
  36400/36400, 50700/50700, итог в п. 11.

## 5. Promote по кольцу (test1: ct1 → ct2 → ct3 → ct1)

Каждый hop: `POST /v1/nodes/{donor}/databases/{db}/promotetomaster` (8 шагов, в
т.ч. `new_guid`), снятие старого мастера (исключение/снятие extra_paths →
scansync → forget → удаление файла), `reinit` нового мастера на следующую
реплику, сравнение `LOADTEST`.

| Hop | Promote | Новый GUID | Reinit дальше | Счёт мастер/реплика |
|---|---|---|---|---|
| ct2b-replica → ct2b-master | ok, 8/8 шагов | 0621F9B8 | → ct3b-replica (PENDING_RESTART → рестарт FB через RCM → IN_SYNC) | 15000 / 15000 |
| ct3b-replica → ct3b-master | ok | F2618E9F | → ct1b-replica | 36400 / 36400 |
| ct1b-replica → ct1b-master | ok | 74FF7636 | → ct2b-replica | 50700 / 50700 |

Возврат домой (путь после трёх hop — четыре уровня вложенности): снятие
extra_paths → forget → `mv` в `/databases/master/test1.fdb` → scansync adopt →
рестарт FB → reinit на ct2b-replica. Adopt сначала не видел файл из-за
`exclude_paths: ["test1.fdb"]`, оставшегося от снятия старого мастера (Ф-6);
`db_file_replaced` поднят верно. Чтобы снять последнюю базу мастера, пришлось
добавить test4 (Ф-3). Donor-строки забываются не сразу (Ф-4).

## 6. Reinit

- Одиночные reinit (standard, `-allow-restart`): 10+ штук, все `ok`; когда
  реплика делит Firebird с мастером, узел сам не перезапускает движок —
  `PENDING_RESTART` + `restart_required_after_reinit`, рестарт через RCM (верно).
- `reinit --all` на ct1b-master (test1 + test4 → ct2b-replica) при
  `limits.max_concurrent_reinits=2` (поставлено через `PUT config`, узел
  перезапущен, `getconfig` показал 2): операция `reinit_all` «succeeded», обе базы
  получили gen 3 на мастере и реплике, **но пары встали**: мастер шлёт первый
  досинхронный сегмент (seq 3146/3301, gen 2), реплика отвечает 410
  `stale_generation` («segment belongs to a superseded database image»), мастер —
  `replication_paused_reinit_scheduled` (serious), NEEDS_REINIT, applied не растёт
  6+ мин. Одиночный `reinit` каждой базы (gen 4) лечит: applied догоняет за минуту
  (Ф-10). Двойной параллелизм как таковой не проверен: обе базы прошли бы за
  секунды.

## 7. Аварии

| Сценарий | Что сделано | Результат |
|---|---|---|
| K1: kill реплики посреди приёма | `systemctl kill -s KILL` ct2b-replica под нагрузкой test1/test4 | systemd поднял службу через 5 с; обе базы IN_SYNC, отправка продолжилась, ложных алертов нет; застарелый `orphaned_database` на этом узле ушёл только после рестарта (Ф-8) |
| K3: kill мастера посреди отправки | kill ct1b-master под нагрузкой | поднялся; отправка возобновилась с ledger, `sequence_conflict` нет; транзитные `retention_blocked_by_apply`, `apply_gap` сняты сами (D-04) |
| K2: kill реплики посреди reinit | reinit test4 → ct2b-replica, через 4 с kill ct2b-replica | job дошёл до `ok` (copying 100 %, seeding, restarting), test4 gen 2 IN_SYNC; реплика отметила `replication_conflict_resolved` («insert … already exists, updating instead») — повторный apply после kill |
| K4: рестарт RCM посреди reinit | reinit test3 → ct1b-replica, через 4 с `systemctl restart hqbirdrcm` | RCM после старта дотянул job до `ok` (F-03/F-10), test3 gen 4 IN_SYNC |
| K5: перенос реплики (C-01) | `POST /v1/databases/{id}/move {"target":"vol2/ct3b-master/test3.fdb"}` на ct1b-replica под нагрузкой (dry run, затем перенос) | 7 шагов, REPLCONFCHANGED, Firebird перезапущен через fbagent, `pending_restart=false`; мастер кратко LAGGING (парковка пары), реплика IN_SYNC по новому пути, отправка продолжилась |

## 8. Реплика офлайн час (ct3b-replica, test2, 11:26–12:26 UTC)

Служба ct3b-replica остановлена в 11:26:06 UTC, нагрузка 75 мин на test1–test4
(у test2 это единственный peer). За час мастер ct2b-master накопил 3300+
неотправленных сегментов (архив 345 МБ), `retry_count` 23, и **не поднял ни одного
алерта** — `/v1/alerts` узла пуст (Ф-11); RCM показывал только `node_stale`
реплики, строка реплики оставалась `IN_SYNC / Ok` (Ф-12). Журнал узла-мастера за
час — только `warn: ship / peer poll … connection refused`.

Возврат: служба запущена 12:26:37; applied реплики 3294 → 10079 за 16 мин
(12:27–12:42, ~7 сегментов/с при нагрузке, шедшей до 12:40); мастер отдал
сегменты из архива, алертов `sequence_conflict` нет. Алерты RCM: `apply_gap`
(ct3b-replica) с 12:27:43 и `retention_blocked_by_apply` (ct2b-master) с 12:28:16
— оба сняты сами в 12:42 (D-04), `node_stale` снят. Строка мастера test2 всё
время догона показывала **FAILED** (12:27:10–12:41:31), IN_SYNC с 12:42:04
(Ф-14). Счётчики LOADTEST после остановки нагрузки равны (п. 11). Архив мастера
через 5 мин после догона — 506 МБ / 10019 файлов (`retention_days = 1`, обрезка
ещё не ожидалась).

## 9. RCM UI и CSRF (сессия браузера через curl)

Логин формой (`username`/`password`, `Origin` + `Sec-Fetch-Site: same-origin`) → 303
и cookie; страницы `/`, `/overview`, `/databases`, `/transfer`, `/alerts`, `/logs`,
`/commands` — 200; `/partials/logs` без сессии — 401; алерты: `ack` → 200
(`acked: true`), `mute` → 200 (`muted_until` +30 мин), `DELETE /v1/alerts/{id}` →
200; `GET /v1/report` — 10.7 КБ (`databases`, `nodes`, `summary`). CSRF (G-01):
POST `/v1/alerts/clear` и `/v1/poll-now` с `Origin: http://evil.invalid` при живой
сессии — **403**, same-origin — 200, логин без `Origin` — 403. Пейджинг Logs
через `/partials/logs` с параметрами страницы не воспроизведён: HTML страницы
содержит один `hx-get="/partials/logs"` без параметров, партиал с `tail=50` вернул
0 строк — проверка G-04 не закрыта этим прогоном. `takeover-replica -dry-run` без
`-master` отвечает `master_required` при единственном мастере копий узла (Ф-13).

## 10. Находки

Серьёзные:

- **Ф-10 (узел).** `reinit --all` под нагрузкой: после смены поколения мастер
  продолжает слать первый не отправленный сегмент старого поколения, реплика
  отвергает его как `stale_generation`, пара встаёт с
  `replication_paused_reinit_scheduled`. Одиночный reinit чистит очередь
  (`reinit_purge_queue`), bulk — нет. Воспроизводимо (две базы из двух).
- **Ф-11 (узел).** Мастер с единственным peer, недоступным час (3300+
  неотправленных сегментов, архив 345 МБ / 6583 файла, `retry_count` 23), **не
  поднимает ни одного алерта**: `/v1/alerts` узла пуст; `backlog_warn/critical`
  считают только отправленное-и-не-применённое (`bytes_pending: 0`). В RCM виден
  только `node_stale` реплики. B-05/B-06 с двумя peer проверить не удалось (Ф-7).
- **Ф-15 (линия выпусков).** RCM, собранный из origin/main (`v2027.5.1`,
  `StateSchemaVersion = 1`), не стартует на `state.json` схемы 2, который записал
  2027.5.0 («newer than this build supports»); агент откатил его на 5.0 и
  заблокировал 5.1. Любая сборка RCM с main без review-цепочки теперь не ставится
  на кластер, где работал 5.0; узел 5.1 (минор 14, схема 2004) стартует и работает
  с RCM 5.0. Слияние review-цепочки в main (минор 17, схема RCM 2) нужно до
  следующего выпуска; `v2027.5.1` из `stable` лучше снять.

Остальное (узел):

- **Ф-1.** `node_permissions_failed` на хосте с двумя узлами: оба пишут один
  probe-файл `.hqcluster_write_probe` в общий `/opt/firebird` (и в общий корень,
  если корни совпали) — гонка «remove …: no such file or directory». Имя файла
  должно включать id узла/pid.
- **Ф-3.** Последнюю базу мастера нельзя снять: `exclude_paths` + scansync →
  409 `scan_would_empty_region` даже с `allow_shrink`; `expect_removed` описан для
  donor-копии. Документированный путь «снять старый мастер после promote» для
  мастера с одной базой не работает — пришлось добавить вторую базу.
- **Ф-6.** `exclude_paths`, который promote пишет donor-узлу, остаётся навсегда:
  следующая reinit на тот же путь даёт ORPHANED «excluded from the managed scan»
  (test1 на ct2b-replica), а снятие старого мастера с тем же приёмом ломает
  последующий adopt (п. 5). Promote/forget должны убирать запись.
- **Ф-7.** Второй peer мастера: `PUT /v1/nodes/{id}/config {"peers":[…]}` → 200,
  `getconfig` показывает peer, `node.json` — нет, reinit → `unknown_peer`;
  `POST /v1/peers` узла пишет в node.json, но precheck реплики → 403, пока peer не
  прописан и на реплике; после рестарта службы узла peer из node.json исчезает
  (на узлах, поставленных агентом из определения кластера goafts, — похоже,
  агент/`launch-install` переписывает node.json). Итог: на кластере, управляемом
  агентом, добавить peer через RCM нельзя; RCM/документация должны это говорить.
- **Ф-8.** Условия не снимаются: `orphaned_database` после возврата базы в
  скан жил до рестарта узла; `replication_paused_reinit_scheduled` (test1, test4
  на ct1b-master) остался после одиночного reinit, который всё починил, и висел
  до конца прогона — 50+ мин при пустой шине алертов узла (п. 11). RCM должен
  снимать условие, когда узел его больше не сообщает.
- **Ф-9.** Reinit, отвергнутый на precheck (`unknown_peer`, 403 реплики), ставит
  мастер-базу в `FAILED` (last_op), хотя она продолжает слать сегменты другим
  peer; состояние держится до следующего успешного reinit.

RCM:

- **Ф-2.** После reinit на узел с Ghost-надгробием той же базы надгробие
  остаётся и прячет живую строку (группа показывает только Ghost при реплике
  IN_SYNC). Снимается `POST /v1/nodes/{держатель}/databases/{db_id мастера}/ghostreplica/clear`;
  с db_id реплики или donor-узлом — 404 (документировать ключ).
- **Ф-4.** `DELETE /v1/nodes/{donor}/databases/{db_id}` после promote несколько
  минут отвечает 409 `mailbox_active` («its mailbox … is live»), хотя старый
  мастер уже не шлёт; документация советует забыть строку сразу.
- **Ф-5.** Promote оставляет файл под `extra_paths` companion-узла, реплика
  зеркалит абсолютный путь: после трёх hop путь реплики
  `/databases/replica/ct1b-master/databases/replica/ct3b-master/databases/replica/ct2b-master/databases/replica/ct1b-master/test1.fdb`.
  Нужен шаг «переместить в корень companion» в promote или плоское имя реплики.
- **Ф-12.** Строка реплики узла в `node_stale` час показывает `IN_SYNC / Ok`
  (applied замер); нужен статус «нет данных».
- **Ф-13.** `takeover-replica -node ct2b-replica -dry-run` без `-master`:
  `master_required: none or several are enrolled`, хотя копии узла от одного
  мастера.
- **Ф-14.** Пока реплика догоняет после часа офлайна (`apply_gap`,
  `retention_blocked_by_apply`), строка мастера test2 15 мин показывала `FAILED`
  (12:27–12:41), хотя мастер исправен и отдаёт архив; IN_SYNC — только когда
  реплика применила всё. Нужно состояние вида `CATCHING_UP`/`LAGGING` или
  пояснение причины в UI.

Движок/окружение: падения Firebird 5.0.5 под fb-loadgen (п. 4) — репорт в команду
движка; `fb-loadgen write-heavy` на employee-базах одноразов.

## 11. Итоговое состояние

Снимок 12:44 UTC после остановки нагрузки (90 с ожидания). Все 6 узлов online,
8 строк баз IN_SYNC, `pending_restart` нет, шины алертов всех узлов пусты.

| База (gen) | Пара | Строк LOADTEST / max(N) на мастере | Реплика |
|---|---|---|---|
| test1 (4) | ct1b-master → ct2b-replica | 1 261 050 / 8073 | равно |
| test2 (3) | ct2b-master → ct3b-replica | 1 667 350 / 6877 | равно |
| test3 (4) | ct3b-master → ct1b-replica | 1 662 400 / 6857 | равно |
| test4 (4) | ct1b-master → ct2b-replica | 888 900 / 8738 | равно |

В RCM осталось 8 алертов, живых условий среди них нет: 2 ×
`replication_paused_reinit_scheduled` (ct1b-master, test1/test4, с 11:51 —
устранены reinit'ами 11:56–12:00, Ф-8), `backpressure_engaged` (ct3b-master
test3, с 11:48, после переноса реплики K5), 3 × `replication_conflict_resolved`
(info, повторный apply после kill), 2 × `firebird_log_error` (warn:
`pthread_mutex_lock … Error code 22` на ct2 с 11:41, `Authentication error` на
ct3 с 11:01; источник не разбирался). Журналы узлов 3.4–8.7 МБ, state.json
1–74 КБ, по 5 `.bak`. fbguard за день: аварийных завершений движка 1/3/1 на
ct1/ct2/ct3 (все — fb-loadgen утром, п. 4), стартов 22/30/18. Версии в конце:
узлы 2027.5.1 (52ca953) × 6, RCM 2027.5.0 (e3ae862). Пароли SYSDBA контейнеров
ротированы после снимка; пароль оператора RCM новый с начала прогона.

## 12. Что не покрыто

- B-05/B-06 с двумя peer у одного мастера и B-07 (`removepeer` → retention) — из-за Ф-7.
- Пейджинг Logs (G-04) через UI — не воспроизведён curl'ом (п. 9).
- `promote_mixed_platform` (Windows-пара), `legacy`/`replconfchain`/`tracegate`
  (дроплеты 2.5/3.0) — вне этого стенда.
- Повтор п. 6–9 на сборке после слияния review-цепочки в main: узел 2027.5.1 из
  main, на котором они шли, не содержит правок этапов 0–3 (п. 2).

## 13. Постскриптум: 2027.5.2 (2026-10-26, 14:23–14:26 UTC)

Review-цепочка влита в main обоих репозиториев (лицензионная пауза — протокол
минор 17, RCM state.json схема 2), выпуск 2027.5.2 подписан ключом зоны EU и
опубликован в `stable` cc2 в 14:23 UTC. Агенты обновили узлы 5.1 → 5.2 в
14:24:52–14:25:06 (по одному хосту, службы перезапущены), RCM 5.0 → 5.2 в
14:25:47 — стартовал на state.json схемы 2 с первого раза. Через 1.5 мин после
публикации все шесть узлов и RCM на 2027.5.2, кольцо IN_SYNC по всем четырём
парам, список алертов RCM после рестарта пуст (восемь застарелых записей п. 11
не вернулись).
