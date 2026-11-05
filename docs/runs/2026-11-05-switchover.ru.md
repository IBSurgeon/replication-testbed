# Переключение мастера и автоматический failover на LXD-стенде ubuntu-1..3 (2026-11-04/05)

Итог: **этапы 1–5 плана переключения работают под нагрузкой**: switchover
туда и обратно, failover после выключения мастера, автоматический failover
при отрезанном мастере; при отрезанном или перезапущенном RCM failover не
начинается. `test switchover` — 19 из 19 (6 переключений) и 14 из 14
(failover и автоматический failover). Найдено и исправлено 7 ошибок узла и
RCM и 1 ошибка test bed. Открыто: Firebird 5.0.5 дважды завис при
переключении (`docs/engine-bugs/hqbird50-shutdown-hang.en.md`).

План: hqcluster-node `docs/cluster-switchover-api-plan.ru.md`.

## 1. Стенд и сборки

| Что | Значение |
|---|---|
| Хосты | LXD: ubuntu-1 (RCM, роутер, нагрузка), ubuntu-2 и ubuntu-3 (сервис `shop` из двух хостов, роутер на каждом); Firebird 5.0.5.1878 HQbird |
| Конфиг | `config/testbed.nogo.local.json`, `TB_STATE_DIR=state/nogo`; `power.off/on` у ubuntu2 и ubuntu3 (`lxc stop --force` / `lxc start`) |
| Узел и RCM | лабораторные сборки 2027.5.8.4 из ветки `switchover` (узел 13dfff7, RCM 1c8e8ea), без подписи |
| Нагрузка | fb-loadgen write-heavy, 2–4 соединения, через роутер ubuntu-1 (`50-load --dsn`) |
| Политика | `failover_delay_sec` 90, `max_data_loss_sec` 30, `min_interval_sec` 3600, `fence_mode route` |

## 2. Ход

| Шаг | Итог |
|---|---|
| Переключения вручную 1–5 (эпохи 4→8) | все ok, 28–29 с, quiesce 12,4–13 с, rejoin ok |
| Выключение ubuntu-3 (мастер), failover вручную | `-check` ok через 14 с, операция 41 с, потеряно 0 сегментов; после включения fenced за ~15 с; rejoin 16 с |
| RCM отрезан от хостов сервиса на 150 с | failover нет: RCM не слышит мастера, но Firebird мастера не видит только 1 роутер из 3 |
| RCM перезапущен | failover нет: «RCM runs for less than failover_delay_sec» 90 с |
| Мастер ubuntu-2 отрезан от всех | автоматический failover через 92 с после последней связи, эпоха +1 за 141 с; правило выключилось само; свидетели: 2 из 3 роутеров, реплика видела мастера 1 мин 43 с назад, отставание 0 с; fenced через 4 с после снятия блокировки; rejoin 11 с |
| `test switchover` (все случаи) | 5 из 14: переключение 2 — `nbackup -L` на новом мастере завис на 30 мин (см. §4), остальные случаи упали из-за сервиса без реплики |
| `test switchover --only switch --rounds 6` | 19 из 19, переключения 25–37 с, quiesce 12,3–12,4 с |
| `test switchover --only failover,rcmcut,rcmrestart,mastercut` | 14 из 14; `-check` ok через 6 с, failover через 42 с после выключения; автоматический failover за 138 с |

Нагрузка шла через роутер всё время и сама переподключалась после каждого
переключения.

## 3. Найдено и исправлено

| # | Где | Что | Исправление |
|---|---|---|---|
| 1 | узел | при quiesce `gfix -shut single` сразу снова пускал нагрузку (SYSDBA через роутер): журнал не успокаивался, мастер оставался в shutdown | `-shut full -force 0` (8494dd4) |
| 2 | RCM | сверка возвращала мастера на старый хост | 5839086 |
| 3 | RCM | запись `ORPHANED` считалась мастером | исправлено в ветке `switchover` |
| 4 | RCM | мастер, записанный RCM, не получал роль и алиас | Announce |
| 5 | RCM | rejoin не освобождал файл старого мастера с `extra_paths` | шаг `rejoin_release` (f5fb445) |
| 6 | узел | после неудачного quiesce база оставалась в shutdown | `gfix -online normal` и ответ, вышла ли база online (13dfff7) |
| 7 | RCM | если и online не удался, сервис оставался «ok» | сервис FAILED с подсказкой «run gfix -online normal on it by hand» (1c8e8ea) |
| 8 | test bed | `hostctl` всегда передавал `--port` (порт Firebird): `block-peer` закрывал только 3050, а не весь адрес | для `block-peer`/`unblock-peer` порт по умолчанию не передаётся |

С ошибкой 8 первые прогоны «RCM отрезан» и «мастер отрезан» закрывали только
Firebird. Это тоже полезный итог: при закрытом порте Firebird и живом узле
мастера RCM failover не начинает.

## 4. Открыто

- **Firebird 5.0.5.1878 HQbird завис дважды** (`docs/engine-bugs/hqbird50-shutdown-hang.en.md`):
  в `gfix -shut full -force 0` на старом мастере (взаимоблокировка, стеки
  gdb) и в `nbackup -L` на новом мастере (стеков нет). Во втором случае
  нагрузка стояла 30 минут.
- `nbackup -L` в узле ограничен 30 минутами; пока `-L` ждёт, ждут все
  пишущие соединения мастера. Нужен короткий предел для `-L`.
- Зависший Firebird не обнаруживается: узел показывает реплику IN_SYNC,
  TCP-проверка HAProxy проходит, автоматический failover не начнётся.
- После failover мастер в `where` — NEEDS_REINIT, пока сервис без реплики.
- Шаг failover «tables»: нет списка публикации ни от реплики, ни от старого
  мастера.
- Старые записи ORPHANED реплик не удалены.
- Не проверены: Windows, HQbird 3.0/4.0, `fence_mode shutdown`.
