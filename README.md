# Garment ERP — производство одежды

Рабочий backend мини-ERP для швейного производства на Python. Модульный монолит:
каталог материалов и моделей → заказ по размерам и цветам → потребность → закупка
и частичная приёмка → резерв материалов → производство → выпуск и отчёт.

## Что работает

- Материалы в метрах и штуках; складской остаток, резерв и свободное количество.
- Рецептура модели (BOM) отдельно для каждой пары размер/цвет, процент технологических потерь.
- Расчёт потребности партии и дефицита. Заказ хранит рассчитанную потребность как снимок.
- Закупки у поставщиков; частичные поставки, запрет сверхприёмки и повторного документа.
- Атомарный резерв всех материалов; при нехватке одной позиции откатывается весь резерв.
- Запуск и завершение производства. Завершение списывает материалы и снимает резерв.
- Отмена черновика или зарезервированного заказа с освобождением материалов.
- Идемпотентные команды, аудит, журнал движения склада.
- Outbox-обработчик с арендой, повторными попытками и состоянием DEAD. Inbox исключает повторное начисление выпуска в отчёте.
- Изоляция фабрик, роли, адаптер проверки JWT Zitadel и авторизации Cerbos.
- OpenAPI/Swagger, request ID, журнал времени запросов, health/readiness endpoints.

## Технологии

Python 3.12+, FastAPI, Pydantic 2, SQLAlchemy 2 async, PostgreSQL/asyncpg,
Alembic, Zitadel, Cerbos, pytest, Docker Compose и Dev Containers.
Точные версии зависимостей зафиксированы в `requirements.txt` и `requirements-dev.txt`.

RabbitMQ/FastStream, Redis, Dishka, Temporal и ClickHouse — следующие этапы;
они **не подключены** в версии 0.1. Для Outbox уже есть порт Publisher.

## Быстрая проверка без сервера и миграций

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install -e . --no-deps
pytest -q
ruff check src tests scripts migrations
ruff format --check src tests scripts migrations
```

Тесты создают временную SQLite-базу, проходят HTTP-контракты через ASGI и удаляют
её вместе с временным каталогом pytest. Они не запускают Alembic, production или CI.
Контроль конкуренции на SQLite не заменяет PostgreSQL-проверку блокировок.

Для PostgreSQL можно прогнать тот же набор, установив `ERP_TEST_DATABASE_URL`
на **отдельную тестовую БД с именем, заканчивающимся на `_test`**.
Каждый тест создаёт и удаляет только собственную случайную схему в этой БД;
таблицы приложения и Alembic не используются.

```bash
ERP_TEST_DATABASE_URL=postgresql+asyncpg://test_user:test_password@localhost:5432/garment_erp_test pytest -q
```

## Локальный запуск в Docker

Нужны Docker и Docker Compose. Скопировать пример конфигурации:

```bash
cp .env.example .env
docker compose up -d db cerbos
```

В `migrations/versions/0001_initial.py` подготовлена исходная миграция. Приложение
и worker **не запускают её автоматически**. Следующая команда изменяет схему
локальной БД; выполнять её только при разрешённой настройке базы:

```bash
docker compose --profile setup run --rm migrate
```

После подготовки схемы:

```bash
docker compose up -d --build api worker
```

Swagger: <http://127.0.0.1:8000/docs>. Нажать **Authorize** и указать значение
`ERP_DEV_TOKEN` из `.env`. Все изменяющие запросы требуют заголовок `Idempotency-Key`.
Dev-режим предназначен для локального запуска; опубликованный пример токена не является production-секретом.
Docker публикует порты API и БД только на `127.0.0.1`.

Можно запустить API и worker без Docker с уже подготовленной PostgreSQL-базой:

```bash
uvicorn garment_erp.api.app:app --host 127.0.0.1 --port 8000
```

В отдельном терминале:

```bash
python -m garment_erp.worker
```

## Демо партии из 100 футболок

Активировать Python-окружение и установить токен для демо-скрипта:

```bash
export ERP_DEV_TOKEN=local-demo-token-change-me
python scripts/demo.py
```

Демо создаёт 60 чёрных футболок размера M и 40 размера L.
Расход ткани: M — 1,2 м; L — 1,4 м; технологические потери — 10%.
Потребность: `(60 × 1,2 + 40 × 1,4) × 1,10 = 140,8 м`.
На складе 80 м; закупка составляет 60,8 м. Приёмка проходит двумя частями —
20 и 40,8 м. Второй документ отправляется повторно с тем же ключом, чтобы
показать отсутствие двойной приёмки. Далее выполняются резерв, запуск и выпуск.

После обработки событий worker отчёт `GET /reports/output` показывает 100
изделий за день в UTC. Если worker ещё не обработал Outbox, отчёт отстаёт.
`python -m garment_erp.worker --drain` обрабатывает доступные сейчас события и завершает работу.
Каждый запуск демо создаёт новую партию с уникальными кодами.

## API

| Область | Методы |
|---|---|
| Материалы | `POST /materials`, `GET /materials`, `GET /materials/{id}` |
| Начальные/внешние остатки | `POST /materials/{id}/receipts` |
| Модели и BOM | `POST /styles`, `GET /styles`, `GET /styles/{id}` |
| Заказы | `POST /orders`, `GET /orders`, `GET /orders/{id}` |
| Потребность | `GET /orders/{id}/requirements` |
| Резерв и отмена | `POST /orders/{id}/reserve`, `POST /orders/{id}/cancel` |
| Производство | `POST /orders/{id}/start`, `POST /orders/{id}/complete` |
| Закупки | `POST /purchases`, `GET /purchases`, `GET /purchases/{id}` |
| Приёмка поставки | `POST /purchases/{id}/receipts` |
| Журнал склада | `GET /inventory/movements` |
| Выпуск по дням | `GET /reports/output` |
| Аудит и доставка событий | `GET /audit`, `GET /operations/outbox` |
| Доступность | `GET /health/live`, `GET /health/ready` |

Количество материалов возвращается строкой с тремя десятичными знаками.
Количество изделий — целое число. Списки ограничены 200 записями; курсорная пагинация
ещё не реализована. Потребность используется для планирования, а актуальная
доступность повторно проверяется атомарно при резервировании.

Повтор команды с тем же ключом, пользователем, фабрикой, маршрутом и телом возвращает
сохранённый результат с `Idempotency-Replayed: true`. Другое тело при том же ключе — 409.
Права проверяются заново и для повторов. Неуспешная транзакция ключ не занимает.
Ошибки бизнес-правил содержат `error.code`, `error.message`, `error.details`, `request_id`.

## Zitadel и Cerbos

Для интеграции создать в Zitadel проект и API-приложение с **JWT access tokens**.
Добавить роли `admin`, `planner`, `warehouse`, `purchaser`, `viewer` и назначить их
пользователям фабрики. Запросить включение project roles и resource-owner organization
в access token; audience должна содержать ID проекта.

Заполнить:

```dotenv
ERP_ENVIRONMENT=production
ERP_AUTH_MODE=zitadel
ERP_ZITADEL_ISSUER=https://your-instance.zitadel.cloud
ERP_ZITADEL_AUDIENCE=your-project-id
ERP_ZITADEL_JWKS_URL=https://your-instance.zitadel.cloud/oauth/v2/keys
ERP_CERBOS_URL=http://cerbos:3592
```

Проверяются подпись RS256, issuer, audience, срок действия и обязательные claims.
Tenant берётся из подписанного `urn:zitadel:iam:user:resourceowner:id`, роли — из
project roles с проверкой принадлежности к той же организации. Заголовки клиента
не задают фабрику или роли. Такая схема MVP предполагает одну фабрику на организацию
и работу пользователей в своей организации. Переключение между внешними фабриками
не реализовано. Opaque tokens/introspection и немедленный отзыв JWT ещё не реализованы.

Политики Cerbos находятся в `cerbos/policies/factory.yaml`.
Недоступность или некорректный ответ identity/authorization provider не даёт доступ.
В production обязательны PostgreSQL, Zitadel и Cerbos; dev-auth запрещён настройками.
Реальное подключение к вашей инсталляции Zitadel/Cerbos выполняется после задания environment.

## Границы первой версии

Есть расчёт и списание планового расхода с заданными потерями, полный выпуск партии
и дневной отчёт. Частичный выпуск, фактический расход/брак, рулоны и партии ткани,
контроль качества, себестоимость, платежи, расписание цеха, frontend и интеграция с 1С
остаются следующими этапами.

Архитектура и соответствие вакансии: [docs/architecture.md](docs/architecture.md).
Результаты проверки: [docs/verification.md](docs/verification.md).
