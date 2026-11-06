# Firebird 5.0.5 HQbird: взаимоблокировка при full shutdown — минимальное воспроизведение

## Кратко

- Сервер `LI-V5.0.5.1878 Firebird 5.0 HQbird` (SuperServer) зависает, когда
  база переходит в **full shutdown** (`gfix -shut full -force 0`), клиенты
  продолжают подключаться к ней, и на сервере есть **пользовательская сессия
  трассировки**, которая появилась **после** старта сборщика мусора этой базы.
- После зависания сервер не отвечает ни на одно новое подключение к любой
  базе. Процесс жив, порт открыт, поэтому guardian и проверка TCP-порта
  ничего не видят.
- Скрипт `hqbird50-shutdown-hang-min.sh` воспроизводит зависание в 13
  запусках из 13, в 1-й или 2-й итерации (меньше минуты на запуск).
- Это тот же цикл, что в зависаниях 1, 4 и 5 из
  [hqbird50-shutdown-hang.en.md](hqbird50-shutdown-hang.en.md). Скрипт
  нашёл два условия, которых там нет (см. «Условия», пункты 2 и 5), и второй
  вариант цикла.
- Попутно найдена вторая ошибка: сервер аварийно завершается при
  `gfix -online normal`, если в этот момент идут отклонённые подключения.
- При проверке обхода `TraceAll = true` найдена третья ошибка: SIGSEGV в
  `jrd_tra::getInterface()` во время `gfix -shut full` (1 раз за 80 итераций).
- Для Windows есть скрипты PowerShell (раздел «Windows»). На Windows цикл
  не даёт зависания: `Thread::waitForCompletion()` ждёт поток GC только 10
  секунд, потом закрытие базы идёт дальше и освобождает память GC, который
  ещё работает. Сервер падает (раздел «Почему на Windows нет зависания»).
  На 1 ядре сервер падает и от третьей ошибки; стеки есть.
- Проверены сборки с исправлениями (раздел «Проверка сборок с
  исправлениями»): исправление третьей ошибки и предложение 4 (вариант
  hvlad) вместе — Linux 15 запусков по 40 итераций, Windows на 1 ядре 4
  запуска по 40 итераций: нет зависаний и падений.

## Проблема

### Цикл шаг за шагом

Номера строк — из дерева 5.0.5.1878 сборки с исправлением (build-id
`8af11883d6c49b7dd58826d291d0c877c1afc099`).

1. `gfix -shut full -force 0` закрывает все подключения и переводит базу в
   full shutdown. Объект `Database` остаётся в памяти, пока есть хотя бы одно
   подключение в процессе. Его поток сборщика мусора (GC) работает и держит
   открытую транзакцию.
2. Новые подключения отклоняются с `isc_shutdown`. `unwindAttach`
   освобождает подключение и вызывает `JRD_shutdown_database`. Эта функция
   сразу возвращается, если есть другие подключения. Потом вызывается
   `trace_failed_attach`.
3. `trace_failed_attach` создаёт временный `TraceManager`. `Database` не
   новая, поэтому `failedAttach = false`. `needs()` → `update_sessions()` →
   `update_session()` для пользовательской сессии трассировки → ветка
   `filename` → `Mapping::setDb()`.
4. `Mapping::ensureCachePresence` (`Mapping.cpp:1326`) находит кэш
   отображений этой базы пустым: `SHUT_database` очищает его
   (`shut.cpp:234`), а в full shutdown он не заполняется. Функция берёт
   `cache->populateMutex` и **с этим мьютексом** подключается к той же базе
   (`DbHandle::attach`, `Mapping.cpp:1331`; встроенный SYSDBA,
   `isc_dpb_map_attach`).
5. Вложенное подключение тоже отклоняется (full shutdown). Если оно
   последнее, его `unwindAttach` вызывает `JRD_shutdown_database`, и та
   по-настоящему закрывает `Database`: держит existence mutex, будит поток GC
   и ждёт его завершения (`pthread_join`). `populateMutex` всё ещё занят этим
   же потоком.
6. Поток GC перед выходом подтверждает свою транзакцию: `TRA_commit` →
   `TraceTransactionEnd` → `needs()` → `update_sessions()` →
   `update_session()` → `Mapping::setDb()` → `ensureCachePresence` → ждёт
   `populateMutex`.
7. Цикл: поток из шага 5 ждёт завершения GC, GC ждёт мьютекс этого потока.
   Таймаутов у этих ожиданий нет.
8. Следующее подключение к базе берёт `dbInitMutex` и ждёт existence mutex.
   Все остальные подключения (к любой базе, включая проверку пароля в
   `security5.fdb`) ждут `dbInitMutex`. Сервер больше не принимает
   подключения.

| Поток | Где стоит | Держит | Ждёт |
|---|---|---|---|
| отклонённое подключение и его вложенное подключение | `trace_failed_attach` → `ensureCachePresence` → `DbHandle::attach` → `unwindAttach` → `JRD_shutdown_database` | `populateMutex`, existence mutex | завершения потока GC |
| сборщик мусора | `garbage_collector` → `TRA_commit` → `update_session` → `ensureCachePresence` | — | `populateMutex` |
| новое подключение к этой базе | `initAttachment` | `dbInitMutex` | existence mutex |
| все другие подключения | `JProvider::internalAttach` (`jrd.cpp:1756`) | — | `dbInitMutex` |

### Второй вариант цикла: кэш системных привилегий

По стекам 11 зависаний: в 9 цикл на `populateMutex` (как зависание 5), в 1
вместо него `Sync` кэша системных привилегий (`spCache`), в 1 — цикл из трёх
потоков с обеими блокировками. Вариант со `spCache`:

- отклонённое подключение: `trace_failed_attach` → `update_session` →
  `Mapping::mapUser` берёт `spCache().getSync()` в режиме `SYNC_EXCLUSIVE` и
  под ним подключается к базе (`Mapping.cpp:1641`) → вложенное подключение
  отклонено → `JRD_shutdown_database` ждёт GC;
- поток GC: `TRA_commit` → `update_session` → `Mapping::mapUser` ждёт тот же
  `Sync` в режиме `SYNC_SHARED` (`Mapping.cpp:1629`).

Цикл из трёх потоков: первый держит `Sync` и existence mutex и ждёт GC
(`Mapping.cpp:1641`); GC ждёт `populateMutex` (`:1326`); второй держит
`populateMutex` и в `DbHandle::attach` (`:1331`) ждёт existence mutex.

Исправление должно закрыть все варианты (см. ниже).

### Условия

Все одновременно:

1. **Пользовательская сессия трассировки** с блоком аутентификации: не
   `trs_admin`, не `trs_system`, и `TraceAll = false` (по умолчанию). Сессия,
   которую SYSDBA запускает по сети (`fbtracemgr`, fbagent), именно такая:
   `TraceSvcJrd` ставит `m_admin = false`, если есть блок аутентификации.
   Сессия HQbird «Firebird Monitoring» — `trs_system` и в цикле не участвует.
2. **Сессия новая для потока GC.** GC читает список сессий один раз, при
   старте (первый `needs()`). Потом `needs()` вызывает `update_sessions()`,
   только если изменился номер изменений хранилища трассировки
   (`ConfigStorage::setDirty`: добавление, удаление сессии, смена флагов).
   А `update_session()` идёт в отображение только для сессии, которой ещё нет
   в списке. Значит, сессия должна появиться после старта GC и до
   shutdown. В кластере так бывает: fbagent запускает сессии трассировки после
   смены роли и перезапуска. Проверено: с одной сессией, запущенной до старта
   GC, нет зависаний за 74 итерации; с новой сессией в каждой итерации —
   зависание в 13 запусках из 13.
3. **Full shutdown**: вложенное подключение SYSDBA тоже отклоняется
   (`jrd.cpp:2054`). В `multi` вложенное подключение разрешено, и цикл не
   образуется: контрольный запуск `SHUT_MODE=multi` без зависания (см.
   результаты).
4. **Поток GC с открытой транзакцией** в старом объекте `Database`: на базе
   был мусор (delete или update с версиями), и GC начал его убирать.
5. **Поток подключений обрывается во время shutdown.** Цикл образуется
   только в момент, когда старый объект `Database` теряет последнее
   подключение, а отклонённое подключение в `trace_failed_attach` держит
   мьютекс. Непрерывный поток подключений держит `Database` открытой. Проверено:
   при постоянной нагрузке файл базы не закрывается (34 итерации без
   зависания), а после остановки нагрузки сервер закрывает его примерно
   через 1 с.

## Как работает скрипт

Файлы (рядом друг с другом):

- `hqbird50-shutdown-hang-min.sh FB_ROOT WORK_DIR ITERATIONS` — основной
  скрипт;
- `hqbird50-shutdown-hang-connects.sh ISQL DATABASE [LOOPS]` — частые
  подключения: LOOPS процессов `isql`, каждый читает бесконечный поток
  команд `CONNECT`, поэтому нет запуска процесса на каждое подключение.

### Отдельный сервер

Скрипт не трогает установленный сервер `FB_ROOT`. Он копирует `FB_ROOT` в
`WORK_DIR/run/fb` и запускает копию как SuperServer:

- `RemoteBindAddress = 127.0.0.1`, свой порт `PORT`, `RemoteAuxPort = 0`;
- свои `FIREBIRD_LOCK` и `FIREBIRD_TMP`, своя копия `security5.fdb`;
- пустой `replication.conf`;
- у копии снят sticky-бит: при `fs.protected_regular` даже root не может
  писать в чужой файл в каталоге со sticky-битом.

Скрипт создаёт `test.fdb` (таблица `T`) и `probe.fdb`.

### Одна итерация

1. Запуск нагрузки:
   - `WRITERS` циклов `isql`: `delete from T where W = n; insert into T
     values (n, 'row'); commit;` каждые 0,05 с — даёт мусор, и GC открывает
     транзакцию;
   - `hqbird50-shutdown-hang-connects.sh` с `CONNECTS` процессами в своей
     группе процессов.
2. Пауза `ONLINE_S` с.
3. Новая сессия трассировки `repro-N` (`fbtracemgr -start` в фоне, конфиг
   `database { enabled = true; log_connections = true }`). Сессия прошлой
   итерации останавливается (`fbtracemgr -stop -id`). Пауза 1 с.
4. Проверка: число транзакций пользователя `Garbage Collector` в
   `MON$TRANSACTIONS` (в логе: `garbage collector transactions: 1`).
   При `INTAKE=closed` здесь прекращаются новые подключения (пауза 1 с).
5. `gfix -shut full -force 0`.
6. Пауза `BURST_S` с: нагрузка идёт, все подключения отклоняются.
7. Остановка нагрузки (флаг-файл писателей и `kill` группы процессов
   подключений).
8. Пауза `SHUT_S` с: старый объект `Database` закрывается. Здесь образуется
   цикл.
9. Проба: `select 1 from rdb$database` в `probe.fdb`. При зависании она не
   получает ответа, потому что новое подключение ждёт `dbInitMutex`.
10. `gfix -online normal` без нагрузки (иначе возможна вторая ошибка, см.
    ниже).

### Обнаружение зависания и диагностика

Каждая утилита запускается с `timeout TIMEOUT`. Если она не ответила, скрипт
сохраняет в `WORK_DIR/hang-<время UTC>/`:

- `stacks.txt` — `gdb -p <pid> -batch -ex 'thread apply all bt'`;
- `core` — `gcore` процесса сервера (около 100 МБ);
- `gstat-h.txt`, `version.txt` (версия, md5 бинарных файлов, ядро), `maps.txt`;
- `firebird.log` и `repro.log`.

Потом скрипт останавливает свой сервер и удаляет `WORK_DIR/run` (кроме
случая `KEEP=1`). Коды выхода: 0 — нет зависания, 2 — зависание, 3 — другая
ошибка, 64 — неверный вызов.

### Параметры (переменные окружения)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `PORT` | 3071 | порт отдельного сервера, только 127.0.0.1 |
| `WRITERS` | 2 | циклы `isql`, которые удаляют и вставляют строки |
| `CONNECTS` | 4 | процессы `isql`, которые только подключаются |
| `ONLINE_S` / `BURST_S` / `SHUT_S` | 3 / 1 / 2 | паузы, с (шаги 2, 6, 8) |
| `SHUT_MODE` | `full` | `multi` — контрольный запуск |
| `TRACE` | `1` | новая сессия в каждой итерации; `once` — одна сессия с начала; `stop` — сессии запускаются только при закрытой `test.fdb`, в каждой итерации одна известная GC сессия останавливается; `0` — без трассировки |
| `INTAKE` | `open` | `closed` — за 1 с до shutdown новые подключения прекращаются (как закрытый HAProxy), открытые остаются до shutdown |
| `FBCONF` | пусто | строки для `firebird.conf` копии, например `TraceAll = true` |
| `TIMEOUT` | 30 | с на одну утилиту; дольше — зависание |
| `KEEP` | 0 | 1 — не останавливать сервер и не удалять файлы |
| `CPUS` | пусто | список CPU для сервера (`taskset -c`), например `0` — 1 CPU |

### Требования и запуск

- root в тестовом контейнере или ВМ; `gdb`. Отладочная информация движка
  (по build-id) нужна только для имён функций в стеках.
- `ISC_PASSWORD` (и `ISC_USER`, по умолчанию SYSDBA) должен подходить к
  `FB_ROOT/security5.fdb`: скрипт копирует эту базу.

```bash
ISC_PASSWORD=... ./hqbird50-shutdown-hang-min.sh /opt/firebird /root/fbmin 40
```

## Результаты

Тестовый контейнер LXD, 2 vCPU, Ubuntu, `/opt/firebird` 5.0.5.1878 HQbird
(сборка с исправлением, build-id выше).

| Вариант | Объём | Результат |
|---|---|---|
| по умолчанию (новая сессия в каждой итерации) | 13 запусков | 13 зависаний: 12 в итерации 1, 1 в итерации 2 |
| `TRACE=once` (одна сессия до старта GC) | 40 итераций | нет зависания |
| одна сессия до старта GC, нагрузка не останавливается | 17 + 4 итерации | нет зависания; оба запуска закончились второй ошибкой при `gfix -online normal` |
| одна сессия, нагрузка останавливается | 34 итерации | нет зависания |
| `SHUT_MODE=multi` | 2 запуска по 30 итераций | нет зависания |
| `CONNECTS=1` (один процесс подключений) | 5 запусков | 5 зависаний, в итерациях 1-4 |
| `FBCONF='TraceAll = true'` | 2 запуска по 40 итераций | нет зависания; запуск 1 закончился третьей ошибкой (SIGSEGV) в итерации 13, запуск 2 — 40 итераций |
| `INTAKE=closed`, `TRACE=stop` | — | не запускались: стенд отдан под проверку сборок с исправлением |

Последние 2 запуска по умолчанию — контроль перед проверкой обходов
(оба зависли в итерации 1).

### Как нашли условия 2 и 5

`bpftrace` с uprobe на функции движка (адреса из отладочного файла сборки):
`JRD_shutdown_database`, `trace_failed_attach`, `ensureCachePresence`,
`DbHandle::attach`, `Cache::populate`, `Database::garbage_collector`,
`TraceManager::update_session`. Трасса показала:

- поток GC вызывает `update_session` один раз, через несколько миллисекунд
  после старта, а при выходе — нет;
- в момент закрытия `Database` картина цикла уже есть: отклонённое
  подключение держит `populateMutex`, его вложенное подключение последнее и
  вызывает `JRD_shutdown_database`, которая по-настоящему закрывает базу. Но GC
  выходит без `update_session` и не ждёт мьютекс.

## Windows: скрипты PowerShell

Папка [hqbird50-shutdown-hang-win](hqbird50-shutdown-hang-win/). Скрипты
написаны для лёгкого изменения, каждая функция описана в комментариях.

- `hang-prepare.ps1` — готовит стенд. Копия `FbRoot` работает как
  приложение `firebird.exe -a` на `127.0.0.1:<Port>`, со своими папками lock
  и tmp, своей `security5.fdb` и `CpuAffinityMask = <CpuMask>`. Скрипт
  создаёт `test.fdb`, `probe.fdb` и файлы `.sql` для нагрузки. Состояние
  стенда в конце подготовки описано в комментарии в начале скрипта.
- `hang-repro.ps1` — итерации, как в `hqbird50-shutdown-hang-min.sh`.
  Зависание: стеки и дамп через `cdb -pv`, код выхода 2. Падение сервера:
  код выхода 3.
- `catch-crash.ps1` — по желанию: подключает `cdb` к серверу стенда. При
  падении `cdb` пишет полный дамп и стеки всех потоков.

Нужны только файлы установленного Firebird 5.0 и `cdb.exe` (Debugging Tools
for Windows). ВМ и Docker не нужны. Установленные службы скрипты не трогают.

Ограничения:

- **Один стенд на хост.** Все процессы Firebird 5 используют общие имена
  объектов ядра (`fb50_trace`). Второй стенд пишет `Wrong file for memory
  mapping`, и подключения к нему не проходят. `hang-prepare.ps1` не
  запускается, если работает другой стенд.
- **ssh.** Windows OpenSSH завершает дочерние процессы, когда сессия
  закрывается, сервер стенда тоже. Подготовку и запуск надо выполнять одной
  командой ssh.

```powershell
$env:ISC_PASSWORD = '<пароль SYSDBA>'
.\hang-prepare.ps1 -FbRoot C:\HQbird\Firebird50 -WorkDir C:\fbrepro -CpuMask 1
.\catch-crash.ps1  -WorkDir C:\fbrepro
.\hang-repro.ps1   -WorkDir C:\fbrepro -Iterations 40
.\hang-prepare.ps1 -WorkDir C:\fbrepro -Teardown
```

### Результаты на Windows (2026-11-06)

Тестовый хост Windows, 16 ядер, копия `WI-V5.0.5.1881` HQbird, параметры
по умолчанию (новая сессия трассировки в каждой итерации).

| `CpuMask` | Объём | Результат |
|---|---|---|
| нет (16 ядер) | 10 итераций | нет зависания |
| 3 (2 ядра) | 40 итераций | нет зависания, нет падения |
| 1 или 4 (1 ядро) | 4 запуска по 30-40 итераций | 3 падения сервера при `gfix -shut full` (итерации 11, 14, 28); в 1 запуске проба подключения вернула ошибку в итерации 5 (причина не разобрана) |

Зависание на Windows не воспроизведено, причина — в разделе «Почему на
Windows нет зависания». Стек падения — в разделе «Третья ошибка».

## Вторая ошибка: падение при `gfix -online normal`

Два раза (итерации 18 и 5, ранняя версия скрипта с постоянной нагрузкой)
сервер аварийно завершился во время `gfix -online normal`. В `firebird.log`:
`pthread_mutex_destroy failed. Error code 16` (EBUSY), затем abort
(`BugCheckAbort = 1`).

Причина по коду (`Mapping.cpp:1326-1334`):

```cpp
MutexLockGuard g2(cache->populateMutex, FB_FUNCTION);
...
bool down = hdb.attach(alias, cryptCb) || cache->populate(hdb);
if (down)
    cache = nullptr;   // g2 ещё держит cache->populateMutex
return down;
```

В full shutdown `down` всегда true. Если в это же время `SHUT_online`
(`shut.cpp:420`) вызвал `Mapping::clearCache` и убрал `Cache` из дерева, то
`cache = nullptr` отпускает последнюю ссылку. Деструктор `Cache` уничтожает
занятый мьютекс → EBUSY → abort. После этого деструктор `g2` разблокировал бы
уже освобождённую память.

## Третья ошибка: SIGSEGV при `gfix -shut full` с `TraceAll = true`

Один раз (`FBCONF='TraceAll = true'`, запуск 1, итерация 13) сервер упал во
время `gfix -shut full -force 0`. `gfix` получил `Error reading data from
the connection`. Сообщение ядра: `segfault ... in libEngine13.so`, смещение
`0x3570f4` = `Jrd::jrd_tra::getInterface(bool)+4` (`tra.cpp:3859`). Это
первая инструкция функции: чтение `tra_interface` (смещение `0x510`) из
объекта `jrd_tra`, память которого уже не отображена, то есть транзакция
уже освобождена. В `firebird.log` только `gds__detach: Unsuccessful detach`
клиентов.

Стека нет: `core_pattern` контейнера передаёт core в apport хоста.
Вызовы `getInterface()` для транзакции: `TRA_release_transaction`
(`tra.cpp:1366`), `checkTranIntf` (`jrd.cpp:413`), `jrd.cpp:4747` (после
`start_transaction`). Какой из них упал, неизвестно. Связь с `TraceAll` не
доказана: без `TraceAll` запуск раньше доходит до зависания. Для стека нужен
`gdb`, подключённый к серверу до падения (`handle SIGSEGV stop`, затем
`thread apply all bt` и `gcore`).

### Стек на Windows

`catch-crash.ps1` поймал падение на Windows (1 ядро, итерация 28, без
`TraceAll`). Упавший поток (access violation):

```
fbclient!Firebird::IReferenceCounted::addRef
fbclient!Why::YEntry<Why::YTransaction>::init
fbclient!Why::YTransaction::validate
fbclient!Why::YAttachment::getTransaction
fbclient!Why::YAttachment::getNextTransaction
fbclient!Why::YAttachment::prepare
firebird!rem_port::prepare_statement
firebird!process_packet
```

`YTransaction` клиента ссылается (`next`) на уже освобождённый
`JTransaction`. В это время другой поток стоит в `JRD_shutdown_database` →
`Thread::waitForCompletion` (ждёт поток GC), вызванной из `unwindAttach`
отклонённого подключения.

Вероятная причина — та же гонка, которую закрывает изменение 3 в fix15t
(`JAttachment::startTransaction`): интерфейс транзакции берётся после того,
как блокировка подключения отпущена. Если forced shutdown успевает удалить
`jrd_tra`, на Linux сразу падает `getInterface()` (страница уже не
отображена). На Windows память кучи ещё читается: `startTransaction`
возвращает освобождённый `JTransaction`, и сервер падает при первом
использовании этой транзакции. Проверено сборкой с исправлением только
третьей ошибки (раздел «Проверка сборок с исправлениями»): за 126 итераций
это падение не появилось. Без исправления сервер падал в 4 запусках из 5
(`1881`: 3 из 4; своя сборка без исправлений: 1 из 1, итерация 8). Стек
есть для двух падений, оба — освобождённый `JTransaction` в
`YTransaction::validate()`.

### Почему на Windows нет зависания

На Windows `Thread::waitForCompletion()` (`common/ThreadStart.cpp:375`)
ждёт поток не дольше 10 секунд:

```cpp
WaitForSingleObject(m_handle, 10000);		// error handler ????????????
detach();
```

На Linux здесь `pthread_join()` без тайм-аута. Поэтому цикл идёт так:

1. `VIO_fini()` ждёт поток GC, а GC ждёт `populateMutex` (цикл, как на
   Linux).
2. Через 10 секунд `VIO_fini()` возвращается. `JRD_shutdown_database()`
   идёт дальше и удаляет `Database`. Деструктор `Database` удаляет все пулы
   памяти базы, в том числе пул подключения GC и его транзакции.
3. Отклонённое подключение отпускает `populateMutex`. GC получает мьютекс и
   работает дальше с уже освобождённой памятью.

Падение на сборке с исправлением только третьей ошибки (1 ядро, запуск 4,
итерация 6). Упавший поток — GC:

```
Engine13!Firebird::TimerImpl::reset
Engine13!Jrd::Attachment::setupIdleTimer
Engine13!Jrd::AttachmentHolder::AttachmentHolder
Engine13!Jrd::JAttachment::startTransaction
Engine13!Jrd::Mapping::Cache::populate
Engine13!Jrd::Mapping::ensureCachePresence
Engine13!Jrd::Mapping::setDb
Engine13!Jrd::TraceManager::update_session
Engine13!Jrd::TraceManager::update_sessions
Engine13!Jrd::TraceManager::needs
Engine13!Jrd::TraceTransactionEnd::TraceTransactionEnd
Engine13!TRA_commit
Engine13!Jrd::Database::garbage_collector
```

Что видно в дампе:

- `Attachment` GC (тот же адрес, что в `garbage_collector`) уже
  освобождён: `att_database`, `att_stable`, `att_trace_manager` равны 0.
  Память его транзакции не читается.
- `SysStableAttachment` GC ещё жив (GC держит на него `RefPtr`) и
  указывает на этот `Attachment`: значит, `Attachment::destroy()` не
  вызывался, память освободил деструктор `Database`.
- По адресу старой `Database` уже новая `Database` той же базы:
  `dbb_sys_attachments` пуст, поток GC не запущен, новое подключение ждёт в
  `CCH_init2`. Ни один поток не стоит в `JRD_shutdown_database()`.
- Новое подключение ждёт `populateMutex`, который держит упавший GC.

Тот же тайм-аут — риск и без цикла: если GC при закрытии базы работает
дольше 10 секунд, `Database` удаляется под работающим GC (по коду, не
проверено).

## Предложения по исправлению

Любое из пунктов 1-4 разрывает цикл. Пункты 1, 3 и 4 закрывают оба
варианта цикла.

1. **Не делать отображение для отклонённого подключения.** В
   `TraceManager::update_session()` не вызывать `Mapping::setDb()` и
   `mapUser()` для любого неудачного подключения, не только для новой
   `Database` (`UNWIND_NEW`); как минимум — после `isc_shutdown`.
   Неудачному подключению всё равно нечего трассировать, кроме события
   отказа.
2. **Не держать блокировки во время подключения к базе.** В
   `ensureCachePresence()` не держать `populateMutex` во время
   `DbHandle::attach()` (или не подключаться, если база в shutdown). То же
   для `Sync` кэша `spCache` в `Mapping::mapUser()` перед
   `mainHandle.attach()`.
3. **Не закрывать `Database` из вложенного подключения.** В `unwindAttach()`
   не вызывать `JRD_shutdown_database()` для подключения с
   `dpb_map_attach`: закрытие оставить внешнему подключению, которое и так
   вызовет `JRD_shutdown_database()`.
4. **Не идти в отображение из последней транзакции GC.** Сделано и
   проверено, вариант hvlad (`hqbird50-shutdown-hang-fix4.diff`):
   - в `JRD_shutdown_database()`, сразу после `dbb_init_fini->destroy()`
     (с этого момента `Database` не используется повторно), для каждого
     системного подключения из `dbb_sys_attachments` (GC, cache writer,
     workers) вызвать `att_trace_manager->stopNewSessions()`;
   - после этого `update_sessions()` не добавляет новые сессии, так же как
     для `att_purge_tid` (`noNewSessions`). Метка в `TraceManager`, поэтому
     работает для подключения любого типа;
   - метка ставится до `VIO_fini()`, поэтому закрыт и `TRA_start` в GC
     (он тоже вызывает `needs()`);
   - трассировка неудачных подключений остаётся (возражение hvlad к
     пункту 1).

   Остаётся только поток GC, который ещё не попал в `dbb_sys_attachments`:
   он ещё не начал транзакцию, поэтому отображения при выходе у него нет.
5. **Вторая ошибка.** Держать собственную ссылку на `Cache`, пока занят
   мьютекс, например:

   ```cpp
   RefPtr<Cache> keep(cache);
   MutexLockGuard g2(keep->populateMutex, FB_FUNCTION);
   ```

   Тогда `cache = nullptr` не уничтожает объект с занятым мьютексом.
6. **Третья ошибка.** В `JAttachment::startTransaction()` перенести всё,
   что идёт после `catch`, внутрь `try` (комментарий AlexPeshkoff):
   интерфейс транзакции берётся, пока подключение заблокировано. Сделано и
   проверено: `hqbird50-shutdown-hang-fix3.diff`.
7. **Тайм-аут 10 секунд на Windows.** `Thread::waitForCompletion()` не
   должен молча идти дальше, если поток не закончился: ждать без
   тайм-аута, как `pthread_join()` на Linux (случай выгрузки DLL уже
   проверяет `dDllUnloadTID`), или хотя бы записать это в `firebird.log`.
   Не сделано, не проверено.

### Обход до исправления

Проверка скриптом: 2 запуска по 40 итераций на вариант (см. «Результаты»).
Проверены не все варианты: стенд отдан под проверку сборок с исправлением.

- `TraceAll = true` в `firebird.conf` (`FBCONF='TraceAll = true'`):
  **проверено, зависаний нет** (80 итераций). `update_session()` пропускает
  отображение. Но в одном запуске сервер упал (третья ошибка, итерация 13).
  Минус: пользовательская сессия трассировки видит подключения всех
  пользователей.
- Перед `gfix -shut full` закрыть приём клиентов (HAProxy), чтобы в базу в
  shutdown не шли отклонённые подключения (`INTAKE=closed`): **не
  проверено**. Подключение, которое началось до закрытия, всё равно может
  быть отклонено.
- Не запускать новые пользовательские сессии трассировки между стартом
  базы и её shutdown: **проверено частично**. Все сессии до старта GC
  (`TRACE=once`): 40 + 34 итерации без зависания. Остановка сессии
  (`TRACE=stop`) не проверена; по коду она цикл не вызывает: новой сессии в
  списке не появляется.

### Как проверить исправление

Запустить скрипт на сборке с исправлением: 10 запусков по 40 итераций без
зависания и без abort. С текущей сборкой тот же запуск зависает в 1-2
итерации.

## Проверка сборок с исправлениями (2026-11-06)

Все сборки — из того же дерева 5.0.5.1878 (build-id `8af11883…` выше),
меняется только `libEngine13.so` / `Engine13.dll`.

| Сборка | Изменения | Linux (`libEngine13.so`) | Windows (`Engine13.dll`, md5) |
|---|---|---|---|
| base | нет | build-id `8af11883…` | `67ed133f…` |
| fix3 | `hqbird50-shutdown-hang-fix3.diff` (третья ошибка) | build-id `72204a66…` | `fdd6ddf8…` |
| fix34 | fix3 + `hqbird50-shutdown-hang-fix4.diff` (предложение 4) | build-id `e09d476c…` | `024799b2…` |

`hqbird50-shutdown-hang-fix4.diff` сделан от исходного дерева, без fix3.
Windows: Visual Studio 2026 (toolset 14.50), к сборке приложены её
`msvcp140.dll` и `vcruntime140*.dll`.

Linux — контейнер LXD, 2 vCPU, `hqbird50-shutdown-hang-min.sh`, 40 итераций
на запуск. Windows — тестовый хост, `-CpuMask 1` (1 ядро),
`catch-crash.ps1`, 40 итераций на запуск.

| Сборка | Где, параметры | Результат |
|---|---|---|
| fix3 | Linux, по умолчанию | зависание в итерации 2 (ожидаемо: fix3 не трогает цикл) |
| base | Linux, `TraceAll = true`, `CPUS=0` | 40 итераций; во 2-м запуске ошибка пробы подключения в итерации 16, без падения |
| fix3 | Linux, `TraceAll = true`, `CPUS=0` | 5 запусков по 40 итераций: нет зависаний и падений |
| base | Windows, 1 ядро | падение в итерации 8 (освобождённый `JTransaction`, третья ошибка) |
| fix3 | Windows, 1 ядро | 3 запуска по 40 итераций; в 4-м запуске падение в итерации 6 — GC после тайм-аута 10 секунд («Почему на Windows нет зависания») |
| fix34 | Linux, по умолчанию | 10 запусков по 40 итераций: нет зависаний и abort |
| fix34 | Linux, по умолчанию, `CPUS=0` | 5 запусков по 40 итераций: нет зависаний и abort |
| fix34 | Windows, 1 ядро | 4 запуска по 40 итераций: нет зависаний и падений |

Выводы:

- fix3 убирает падение с освобождённым `JTransaction`. На Linux сравнение
  слабое: base не упал за 56 итераций. На Windows сильнее: без fix3 4
  падения в 5 запусках, с fix3 — ни одного такого.
- fix34 убирает цикл: на Linux исходная сборка зависает в итерации 1-2, с
  fix34 — 600 итераций без зависания. На Windows убирает и падение после
  тайм-аута: GC больше не делает отображение при выходе.
- Вторая ошибка (`hqbird50-shutdown-hang-min.sh` видит её как abort) в этих
  запусках не появилась; в fix34 её исправления нет.
- Ошибка пробы подключения не разобрана: Linux base
  (`connection rejected by remote interface`) и раньше один раз на Windows
  `1881`. Сервер в это время не падал.

Изменение в скриптах Windows: `hang-prepare.ps1 -Teardown` сначала
останавливает `cdb` из `catch-crash.ps1`. Сервер под отладчиком не
завершается, пока работает `cdb`, а `cdb` держит открытыми DLL сервера, и
каталог стенда нельзя удалить.
