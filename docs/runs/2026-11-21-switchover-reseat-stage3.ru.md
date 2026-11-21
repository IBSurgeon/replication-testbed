# 2026-11-21: третьи реплики при switchover без reinit (этап 3, лаборатория)

План: hqcluster-node `docs/switchover-keep-replicas-plan.ru.md`, §8 этап 3.
Тест: `tests/reseat.py` (`tb.py test reseat`), описание в `docs/tests.md`.

## Итог

**На 5.0 и 4.0 всё, что ждёт план, работает.**

- Цикл из 40 переключений A→B→C→A под нагрузкой fb-loadgen: 40/40 ok на
  каждой версии. Каждая третья реплика получила класс `ready` и результат
  `done` (перепривязка на месте), на чётных и нечётных переключениях.
  Ни одного reinit третьей реплики, ни одного `NEEDS_REINIT` после
  перепривязки, три файла равны после каждого переключения.
- kill node третьей реплики на каждом из 6 шагов перепривязки: повтор по
  свидетелю (повтор запроса RCM) перепривязал C на месте, без reinit,
  после `-F` только вперёд. 6/6 на каждой версии.
- behind, seg1: PASS на обеих версиях.
- noagent: класс `ready` при apply, перепривязка отказана
  `409 fbagent_required`, итог `failed` с предупреждением. Переключение ok,
  молчаливого расхождения нет, reinit возвращает C.
- nolink: класс `unknown` с предупреждением, C не перепривязана, node C
  показывает `NEEDS_REINIT`. Переключение ok, молчаливого расхождения нет,
  reinit возвращает C.
- Ошибка 6 (4.0.8, new_guid, `gfix -online normal`: «database shutdown»)
  и `still_replica` в повторном прогоне не появились (около 50
  переключений на 4.0).

## Сборки и стенды

- node 2027.5.16.4 = hqcluster-node `fae2b21`; RCM 2027.5.16.3 =
  hqcluster3 `2dc141e`. Лабораторные сборки, не релиз.
- 5.0: HQbird Firebird LI-V5.0.5.1878; 4.0: HQbird Firebird LI-V4.0.8.3319.
- Два стенда по три временных LXD-контейнера (Ubuntu 24.04): A, B, C, на
  каждом node master и node replica, fbagent, RCM на A. Связи master →
  replica между каждыми двумя хостами. После прогона контейнеры удалены.
- Стенд 4.0 собран заново перед прогоном (после ручного ремонта в первом
  прогоне).

## Результаты

| Случай | 5.0 | 4.0 |
|---|---|---|
| Цикл, 40 переключений | 40/40: ready / done, без reinit | 40/40: ready / done, без reinit |
| behind | PASS: класс behind, reinit `4594a22f` | PASS: класс behind, reinit `7b80b510` |
| seg1 | PASS: разрыв, C ничего не применяет, reinit | PASS |
| kill × 6 шагов | 6/6: перепривязана повтором | 6/6: перепривязана повтором |
| noagent | PASS (+1 SKIP, открытый пункт RCM) | PASS (+1 SKIP) |
| nolink | PASS (+1 SKIP, открытый пункт RCM) | PASS (+1 SKIP) |

Строки результатов:

- 5.0: часть 1 (22 переключения) 66 PASS; часть 2 (18 переключений и все
  негативные) 110 PASS, 1 FAIL, 2 SKIP; повтор noagent 4 PASS, 1 SKIP.
  Часть 1 остановил тест-бед: первый запрос fb-loadgen не дождался ответа
  на загруженном хосте (load average ≈ 14). Тест теперь повторяет запуск
  нагрузки.
- 4.0: 176 PASS, 1 FAIL, 2 SKIP; повтор noagent 4 PASS, 1 SKIP (операция `3240d7b9`).
- FAIL в обоих прогонах — ожидание теста, не продукт: после отказа
  перепривязки RCM пишет в итоговый документ класс `failed`, а класс при
  apply (`third replicas: … ready`) остаётся в шаге apply. Тест исправлен
  (`dab5fa7`), повтор noagent прошёл.

Примеры операций (5.0 / 4.0): kill at witness `11d71fec` / `7b7c9e59`,
noagent `31798647` / `2c900e27`, nolink `396304f8` / `5697f6eb`.

## Найдено в первом прогоне и исправлено

Первый прогон: node `283f388`, RCM `4de4f8f`, 12 переключений цикла и
негативные случаи на каждой версии.

1. node: `NEEDS_REINIT` после успешной перепривязки (сегменты G2 считались
   чужими до рестарта Firebird). Исправлено в node `6d8ec04`.
2. node: повтор по свидетелю всегда падал на `gfix -shut single` («Target
   shutdown mode is invalid»), база уже в single shutdown. Исправлено в
   node `9d57d15`.
3. RCM: когда new_guid пропущен, G2 не известен, каждая вторая третья
   реплика шла в reinit. Исправлено в RCM `2dc141e` (G2 и sequence из
   `GET /guid`).
4. RCM/node: reinit после прерванной перепривязки отказан
   `target_not_replica`. Исправлено на стороне node в `9d57d15`.
5. RCM: из трёх попыток перепривязки сохранялась только последняя ошибка.
   Исправлено в RCM `2dc141e`.
6. node: перепривязка шла без работающего fbagent (проверялась только
   настройка). Исправлено в node `9661b6c` (запрос к fbagent, `409
   fbagent_required`).

## Открытые пункты

- RCM: finishThirds не повторяет reinit третьей реплики. noagent: reinit
  сразу после операции падает («stop firebird before replacing an existing
  database», fbagent ещё не работает). nolink: reinit не доходит, пока
  связь не вернулась. C возвращает только reinit по команде. В тесте это
  SKIP.
- noagent: node C в момент проверки показывает `IN_SYNC` (старый GUID),
  хотя ничего не применяет. Видно по классу `failed` и предупреждению RCM.
- 4.0.8: ошибка 6 (new_guid, `gfix -online normal`: «database shutdown»,
  первый прогон, операция `cefa1636`) и `still_replica` (первый прогон,
  после ручного seed с overwrite) больше не появились. Причина ошибки 6 не
  найдена.

## Запуск

```sh
python tb.py --config config/<stand>.local.json test reseat --only setup --service <id>
python tb.py --config config/<stand>.local.json test reseat --service <id>
```

Результаты: `state/<stand>/results/reseat-<время>/` (results.json,
summary.md, документы операций op-*.json).
