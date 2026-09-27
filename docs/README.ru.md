# Стенд репликации: краткое руководство

Скрипты-модули собирают кластер репликации Firebird (hqclusternode + fbagent +
hqbirdrcm) на реальных Linux- и Windows-хостах, дают нагрузку и проверяют его.

- **Модули** (`modules/linux/*.sh`, `modules/windows/*.ps1`) работают на хосте.
  У каждой установки есть парная команда удаления.
- **`tb.py`** работает на машине оператора: читает локальный конфиг, копирует
  модули на хосты по ssh и запускает их.
- **Тесты** (`tests/*.py`) — команды `tb.py test <имя>`.

По умолчанию: один мастер и **две реплики на разных ВМ**, RCM на хосте мастера.

## Секреты

Реальные хосты, адреса, пользователи, URL, pin и пароли хранятся **только** в
`config/testbed.local.json` (в git не попадает). В репозитории — только
`config/testbed.example.json` с заглушками `<...>`. Перед push:
`python tools/secret_scan.py`.

## Порядок работы

```bash
cp config/testbed.example.json config/testbed.local.json   # заполнить
python tb.py check                               # конфиг, ssh, Firebird на хостах
python tb.py install --source local              # п.1: из локальных копий, без enroll в goafts
python tb.py install --source goafts             # п.2: всё из goafts (url + pin в конфиге)
python tb.py dbs prepare --count 2 --subdir tb   # п.3: базы на мастере + первая копия на реплики
python tb.py loadgen deploy --from git           # п.4: сборка fb-loadgen, копия на мастер, проверка 5 с
python tb.py loadgen deploy --from local --target local   # п.4: из локальной копии, на этой машине
python tb.py load start --db all --mode mixed --tx emul-safe   # п.5: нагрузка
python tb.py test reinit_cycles --cycles 10      # п.7: много реинициализаций под нагрузкой
python tb.py test disasters                      # п.8: аварии
python tb.py dbs remove                          # удалить тестовые базы
python tb.py uninstall --source local            # удалить установленное (goafts: --source goafts --deregister); проверяет остатки
python tb.py hosts wipe --hosts all --yes        # полная очистка хостов от всего, что поставил стенд
```

Порты Firebird и fbagent можно не задавать: `install` берёт `RemoteServicePort`
из `firebird.conf` (иначе 3050) и `local_api.listen` существующего агента
(иначе 13055). CSR на goafts одобряется только при точном совпадении имени
хоста, адреса источника и времени, и если такой запрос один.

П.6 (реплики) — те же команды установки: `--hosts replicas` или `--hosts replica1`.

## Режимы нагрузки

| Параметр | Значения |
|---|---|
| `--mode` | `write`, `read`, `mixed` (write + read на каждую базу), `spike`, `oltp-emul` |
| `--tx` | `off` — без смены транзакций; `emul-safe`, `full` — со сменой транзакций |
| `--db` | `all`, `db1`, `db1,db2` |
| `--conns` | `MIN:MAX` соединений на процесс |
| `--minutes` | `0` — до `load stop` |
| `--limbo` | разрешить limbo (по умолчанию выключено) |

## Новые тесты

Новые тесты добавляются в этот репозиторий: `tests/<имя>.py`, действия на
хосте — в модули для Linux и Windows. Подробно: [adding-tests.md](adding-tests.md).
