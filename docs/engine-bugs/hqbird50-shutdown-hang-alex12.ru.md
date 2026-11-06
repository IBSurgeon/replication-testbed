# Firebird 5.0.5 HQbird: проверка патча 12.patch (AlexPeshkoff), 2026-11-06

Отчёт для IBSurgeon/hqbird#12. Общее описание цикла, скрипты и другие
сборки — в [hqbird50-shutdown-hang-repro.ru.md](hqbird50-shutdown-hang-repro.ru.md).

## Кратко

- Проверена сборка 5.0.5.1878 **только** с патчем AlexPeshkoff
  (`12.patch`, файл [hqbird50-shutdown-hang-alex12.patch](hqbird50-shutdown-hang-alex12.patch)),
  без других исправлений.
- **Linux: зависание в итерации 1 в 2 запусках из 2** (по умолчанию и на 1
  CPU). Исходная сборка зависает так же (итерация 1-2).
- Оба зависания — **вариант цикла со `spCache`** (второй вариант): патч его
  не меняет. Вариант с `populateMutex` в стеках больше не появляется.
- **Windows (1 ядро): падение в 4 запусках из 4** (итерации 40, 25, 13,
  24). Все 4 — третья ошибка (освобождённый `JTransaction`), как у исходной
  сборки: патч её не трогает. Падение скрывает, что будет с циклом на
  Windows.
- Вывод: патч закрывает вариант с `populateMutex` (и, по коду, вторую
  ошибку), но не цикл в целом. Нужно то же изменение для `Sync` кэша
  `spCache` в `Mapping::mapUser()`, или предложение 4
  (`hqbird50-shutdown-hang-fix4.diff`), или оба.

## Патч

`Mapping::ensureCachePresence()` (`Mapping.cpp:1321-1342`): подключение к базе
(`hdb.attach()`) теперь идёт **до** захвата `populateMutex`. Под мьютексом
остаются только повторная проверка `dataFlag` и `cache->populate()`.
`cache = nullptr` выполняется после того, как мьютекс отпущен.

```diff
-	MutexLockGuard g2(cache->populateMutex, FB_FUNCTION);
-	if (cache->dataFlag)
-		return false;
-
-	// Create db attachment if missing it and populate cache from it
-	bool down = hdb.attach(alias, cryptCb) || cache->populate(hdb);
-	if (down)
-		cache = nullptr;
-	return down;
+	if (!hdb.attach(alias, cryptCb))
+	{
+		MutexLockGuard g2(cache->populateMutex, FB_FUNCTION);
+		if (cache->dataFlag)
+			return false;
+
+		// Create db attachment if missing it and populate cache from it
+		if (!cache->populate(hdb))
+			return false;
+	}
+
+	cache = nullptr;
+	return true;
```

Патч применился к дереву 5.0.5.1878 (build-id `8af11883…`) без смещений.
Другие файлы дерева — исходные (проверено сравнением с копиями
`*.1878fix`).

## Сборки

| Система | Файл | md5 | Прочее |
|---|---|---|---|
| Linux | `libEngine13.so` | `20f22e8fd0585f7939bea8c8152a3b72` | build-id `4b1431adc86a0d93212ad124bce39af3cf3cb9ce`; отладочный файл md5 `9187a54ff7ab5e02baa0b977ef31da02` |
| Windows | `Engine13.dll` | `945d3899c009e39c744caf8813fd42a6` | Visual Studio 2026 (toolset 14.50), Release x64, `Engine13.pdb` рядом |

Остальные файлы сервера — те же, что для сборок base, fix3 и fix34
(Linux: 5.0.5.1878 HQbird, Windows: копия `WI-V5.0.5.1881` HQbird),
меняется только движок.

## Как проверяли

Те же скрипты и тот же объём, что для сборки fix34 (исправление третьей
ошибки + предложение 4):

- **Linux**: контейнер LXD, 2 vCPU, Ubuntu,
  `hqbird50-shutdown-hang-min.sh`, 40 итераций на запуск, отдельный сервер
  на порту 3071. План: 10 запусков с параметрами по умолчанию (новая
  пользовательская сессия трассировки в каждой итерации), затем 5 запусков
  с `CPUS=0` (сервер на 1 CPU). План останавливает шаг на первом
  зависании.
- **Windows**: тестовый хост, 16 ядер, `hang-prepare.ps1 -CpuMask 1`
  (сервер на 1 ядре), `catch-crash.ps1` (полный дамп при падении),
  `hang-repro.ps1 -Iterations 40`, 4 запуска. Каждый запуск — отдельный
  шаг: падение не останавливает следующие.

## Результаты на Linux

| Шаг | Запуск | Результат | Как обнаружено |
|---|---|---|---|
| по умолчанию | 1 из 10 | **зависание в итерации 1** (65 с от старта) | проба подключения к `probe.fdb` без ответа 30 с |
| `CPUS=0` | 1 из 5 | **зависание в итерации 1** (57 с от старта) | `gfix -shut full -force 0` без ответа 30 с |

Оба шага остановились на первом запуске (так задан план). Abort (`pthread_mutex_destroy`,
`BUGCHECK`) нет. В `firebird.log` только запуск сервера.

Для сравнения, тот же план на fix34: 10 + 5 запусков по 40 итераций без
зависаний.

### Стек зависания (Linux, запуск по умолчанию)

16 потоков, `gdb` и core сохранены. Цикл из двух потоков:

Поток 13 — отклонённое подключение, держит `Sync` кэша `spCache` в режиме
`SYNC_EXCLUSIVE` и под ним подключается к базе:

```
Thread::waitForCompletion            ThreadStart.cpp:198   (pthread_join потока GC)
JRD_shutdown_database
unwindAttach                         jrd.cpp:8910
JProvider::internalAttach            вложенное подключение отклонено
JProvider::attachDatabase
Mapping::DbHandle::attach            Mapping.cpp:200
Mapping::mapUser                     Mapping.cpp:1646   (mainHandle.attach под SYNC_EXCLUSIVE)
TraceManager::update_session         TraceManager.cpp:355
TraceManager::update_sessions        TraceManager.cpp:251
TraceManager::needs
trace_failed_attach                  jrd.cpp:1601
unwindAttach
JProvider::internalAttach            подключение клиента, isc_shutdown
```

Поток 5 — GC, последняя транзакция, ждёт тот же `Sync` в режиме
`SYNC_SHARED`:

```
Sync::lock
Mapping::mapUser                     Mapping.cpp:1634
TraceManager::update_session         TraceManager.cpp:314
TraceManager::update_sessions
TraceManager::needs
TraceTransactionEnd::TraceTransactionEnd
TRA_commit                           tra.cpp:447
Database::garbage_collector
```

Остальные потоки ждут за ними: 2 отклонённых подключения ждут тот же `Sync`
(`Mapping.cpp:1634`), 3 подключения ждут `dbInitMutex` (`jrd.cpp:1756`), 1 —
existence mutex `Database` (`jrd.cpp:527`).

Запуск с `CPUS=0` (17 потоков) — тот же цикл: поток в `mapUser`
(`Mapping.cpp:1646`) → `DbHandle::attach` → `JRD_shutdown_database`, GC в
`mapUser` (`Mapping.cpp:1634`), ещё 3 потока ждут тот же `Sync`
(`Mapping.cpp:1634` и `:1642`).

### Почему теперь зависает сразу

`TraceManager::update_session()` сначала вызывает `Mapping::setDb()`, а
он — `ensureCachePresence(dbCache, ..., mainHandle, ...)`. В full shutdown
вложенное подключение там отклоняется, `mainHandle` остаётся пустым, и
`ensureCachePresence()` возвращает «база недоступна» — теперь без
блокировки. Потом `mapUser()` доходит до системных привилегий
(`Mapping.cpp:1627-1656`) и снова пробует `mainHandle.attach()`, уже под
`spCache` в режиме `SYNC_EXCLUSIVE`. Это подключение тоже отклоняется,
оно последнее и вызывает `JRD_shutdown_database()`, которая ждёт GC. GC
при выходе проходит `ensureCachePresence()` (у него своё подключение) и
ждёт этот `Sync`.

До патча цикл почти всегда (9 из 11 зависаний) замыкался раньше, на
`populateMutex`. Патч убирает этот вариант, и цикл замыкается на
следующей блокировке. Это вторая половина предложения 2 в
[hqbird50-shutdown-hang-repro.ru.md](hqbird50-shutdown-hang-repro.ru.md)
(«То же для `Sync` кэша `spCache`»): патч её не делает.

Номера строк `Mapping.cpp` в этом отчёте — из дерева с патчем: после
`ensureCachePresence()` они на 5 больше, чем в исходном дереве
(`:1646` здесь = `:1641` в основном документе).

## Результаты на Windows (1 ядро)

| Запуск | Результат | Где упал |
|---|---|---|
| 1 | падение в итерации 40 | `gfix -shut full` |
| 2 | падение в итерации 25 | `gfix -shut full` |
| 3 | падение в итерации 13 | `gfix -shut full` |
| 4 | падение в итерации 24 | `gfix -shut full` |

Все 4 падения — одно и то же место, третья ошибка: поток клиента в
`prepare` берёт транзакцию, а `JTransaction` уже освобождён вместе с пулом
подключения.

```
Engine13!Firebird::MemPool::releaseBlock          (mutex / счётчик в освобождённом пуле)
Engine13!operator delete
Engine13!Jrd::JTransaction::release
fbclient!Why::YEntry<Why::YTransaction>::fini
fbclient!Why::YTransaction::validate
fbclient!Why::YAttachment::getTransaction
fbclient!Why::YAttachment::prepare
firebird!rem_port::prepare_statement
firebird!process_packet
```

Это то же место, что у исходной сборки (падение в 4 запусках из 5) и у
`1881`: `YTransaction::validate()` с освобождённым `JTransaction`. Там
падение было в `addRef()` (`YEntry::init`), здесь — в `release()`
(`YEntry::fini`): счётчик ссылок дошёл до нуля, и `delete` идёт в уже
освобождённый пул.

В момент падения ни один поток не стоит в `Mapping::mapUser()`,
`JRD_shutdown_database()` или `waitForCompletion()` (у исходной сборки
один поток ждал GC в `JRD_shutdown_database()`). Потоки GC ждут работы
или пишут страницы (`CCH_flush`). Одно отклонённое подключение (запуски
2 и 4) делает вложенное подключение в `ensureCachePresence()` уже без
`populateMutex` — так, как задумано патчем. По времени шагов признаков 10-секундного ожидания GC
(`Thread::waitForCompletion()`) нет ни в одной из 98 завершённых
итераций.

Вывод для Windows: без исправления третьей ошибки сервер падает раньше,
чем можно оценить цикл. Чтобы проверить патч на Windows, нужна сборка
`12.patch` + `hqbird50-shutdown-hang-fix3.diff`. По Linux ожидаем там цикл
со `spCache`, а на Windows он превращается в падение GC после 10 с
ожидания (как у fix3 в 4-м запуске).

Полные дампы (`crash-alex12-N.dmp`, 210-260 МБ) и вывод `cdb` остаются на
тестовом хосте.

## Сравнение сборок (тот же скрипт и объём)

| Сборка | Linux, по умолчанию | Linux, 1 CPU | Windows, 1 ядро |
|---|---|---|---|
| без исправлений (1878) | зависание в итерации 1-2 (13 из 13) | — | падение в 4 запусках из 5 (освобождённый `JTransaction`) |
| fix3 (третья ошибка) | зависание в итерации 2 | 5 × 40 без зависаний (`TraceAll = true`) | 3 запуска без падений, 1 падение GC после 10 с ожидания |
| fix34 (fix3 + предложение 4) | 10 × 40 без зависаний | 5 × 40 без зависаний | 4 × 40 без зависаний и падений |
| **12.patch** | **зависание в итерации 1** | **зависание в итерации 1** | **падение в 4 из 4** (третья ошибка) |

## Что предлагаем

1. **То же изменение для `spCache` в `Mapping::mapUser()`.** Не держать
   `Sync` в режиме `SYNC_EXCLUSIVE` во время `mainHandle.attach()`:
   подключиться до захвата, под блокировкой только проверить кэш ещё раз и
   заполнить его. Например (не проверено):

   ```cpp
   if (!spCache().getPrivileges(mainDb, name, sqlRole, trustedRole, *systemPrivileges))
   {
       sync.unlock();

       // no lock while attaching: the attachment can wait for the GC
       mainHandle.attach(mainAlias, cryptCallback);

       sync.lock(SYNC_EXCLUSIVE);
       if (mainHandle &&
           !spCache().getPrivileges(mainDb, name, sqlRole, trustedRole, *systemPrivileges))
       {
           spCache().populate(mainDb, mainHandle, name, sqlRole, trustedRole);
           spCache().getPrivileges(mainDb, name, sqlRole, trustedRole, *systemPrivileges);
       }
   }
   ```

2. **Предложение 4 (вариант hvlad) вместе с патчем.** Уходящее системное
   подключение не берёт новые сессии трассировки, и GC при выходе не идёт в
   отображение совсем. Это закрывает оба варианта цикла независимо от
   блокировок в `Mapping.cpp` (проверено: fix34, 15 + 4 запуска по 40
   итераций). AlexPeshkoff в #12 тоже назвал это правильной доработкой.
3. **Исправление третьей ошибки** (`hqbird50-shutdown-hang-fix3.diff`)
   нужно в любом случае: без него на Windows сервер падает в
   `YTransaction::validate()` на освобождённом `JTransaction`.

Сборку «12.patch + изменение для `spCache` + fix3» (и, если нужно, +
fix4) можем проверить тем же планом. Без fix3 Windows падает на третьей
ошибке раньше, чем можно оценить цикл.

## Файлы

| Файл | Что это |
|---|---|
| [hqbird50-shutdown-hang-alex12.patch](hqbird50-shutdown-hang-alex12.patch) | патч AlexPeshkoff, как в #12 |
| [hqbird50-shutdown-hang-min.sh](hqbird50-shutdown-hang-min.sh) | скрипт Linux |
| [hqbird50-shutdown-hang-win/](hqbird50-shutdown-hang-win/) | скрипты Windows |
| [hqbird50-shutdown-hang-fix3.diff](hqbird50-shutdown-hang-fix3.diff), [hqbird50-shutdown-hang-fix4.diff](hqbird50-shutdown-hang-fix4.diff) | сборки fix3 и fix34 для сравнения |

Стеки, core и логи зависаний остаются на тестовом хосте; можем передать по
запросу.
