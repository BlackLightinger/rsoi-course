# AeroFlow — поиск и бронирование авиабилетов

Рабочий микросервисный прототип на **Python/FastAPI + React**. В составе: собственный OpenID Connect Identity Provider, публичный поиск, защищённые бронирования и оплаты, бонусная программа, Kafka-аудит и административный отчёт, ограниченный partner API, Docker Compose, Kubernetes + NGINX Ingress и CI/CD.

## Быстрый запуск

Требуются Docker и Docker Compose.

```bash
cp .env.example .env
docker compose up --build
```

Откройте [http://localhost](http://localhost). Демо-администратор: `admin` / `admin123` (если `ADMIN_PASSWORD` не изменён). Первичная сборка поднимает 8 контейнеров приложений плюс Kafka, поэтому может занять несколько минут.

Публичный поиск работает без входа и подсказывает города при вводе. Если подсказки временно недоступны, интерфейс деградирует до обычного ручного ввода города. Бронирование, оплата, личный кабинет, выбор мест/багажа и онлайн-регистрация требуют OIDC-вход. У администратора появляется вкладка статистики и CSV-отчёт.

Пользовательскую учётную запись можно создать через кнопку «Регистрация» в шапке сайта; IdP затем использует этот логин и пароль в стандартном Authorization Code + PKCE flow.

## Сервисы

- Web UI — React/Vite, порт `80`;
- API Gateway — агрегация и orchestration, порт `8000`;
- Identity Provider/User Service — OIDC и пользовательские данные, порт `8001`;
- Flights, Tickets, Loyalty, Payments, Statistics — порты `8002`–`8006` для локальной диагностики;
- Kafka — доступна только внутри compose-сети.

У каждого stateful-сервиса отдельный volume и отдельная SQLite БД. Схема взаимодействий, границы данных, авторизация и сценарии деградации описаны в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Ошибки, логирование и статистика

Все сервисы подключают общий observability middleware: HTTP-запросы, ошибки валидации, необработанные исключения и доменные действия логируются структурированно и публикуются в Kafka topic `service-events`. Ответы об ошибках возвращаются в JSON с `request_id`, чтобы запись в логах и событие в статистике можно было связать с пользовательским запросом.

Kafka считается некритичной зависимостью для пользовательского сценария: если брокер недоступен, запрос не падает, а событие остаётся в приложенческом логе. Statistics service читает события из Kafka в собственную БД и отдаёт администратору отчёт через Gateway в JSON и CSV.

## Проверка API

Публичный запрос через Gateway:

```bash
curl --get 'http://localhost/api/flights' \
  --data-urlencode 'origin=Москва' \
  --data-urlencode 'destination=Санкт-Петербург' \
  --data-urlencode 'passengers=1'
```

Подсказки городов:

```bash
curl --get 'http://localhost/api/cities' \
  --data-urlencode 'query=моск'
```

Ограниченный API для стороннего приложения:

```bash
curl --get -H 'X-API-Key: partner-demo-key' 'http://localhost/partner/v1/flights' \
  --data-urlencode 'origin=Москва'
```

Swagger доступен на `http://localhost:8000/docs` (Gateway) и на диагностических портах доменных сервисов. Прямые бизнес-запросы к ним без Bearer JWT возвращают `401`.

## Локальная разработка без контейнера UI

```bash
python3 -m pip install -e '.[test]'
cd frontend && npm install && npm run dev
```

Python-сервисы запускаются командой вида:

```bash
KAFKA_ENABLED=false DATA_DIR=./work/dev-data uvicorn services.idp.app:app --port 8001
```

Для полного сценария проще использовать Compose: Vite уже проксирует `/api` и `/oauth2` на нужные локальные порты.

## Тесты и сборка

```bash
make test
docker compose config
kubectl kustomize k8s/base >/dev/null
```

GitHub Actions выполняет Python-тесты, TypeScript/Vite build и проверку Kustomize, затем собирает и публикует отдельный образ каждого сервиса в GHCR. На push в `main` pipeline применяет Kubernetes-манифесты, обновляет images всех deployment’ов на immutable tag текущего commit SHA и дожидается rollout. Для deployment нужны secrets `KUBE_CONFIG`, `ADMIN_PASSWORD`, `GATEWAY_CLIENT_SECRET`, `PARTNER_API_KEY` и environment variable `PUBLIC_URL` (например, `https://flight.example.com`). Подробности — в [docs/CI_CD_KUBERNETES.md](docs/CI_CD_KUBERNETES.md).

## Kubernetes

В кластере должен быть установлен NGINX Ingress Controller и default StorageClass.

```bash
kubectl apply -k k8s/base
```

Базовый host — `flight.local`. Перед реальным развёртыванием замените host/public URL и development secrets. Stateful-сервисы имеют по одному replica из-за SQLite; Gateway и Web — по два. NetworkPolicy закрывает прямой вход в доменные сервисы, оставляя маршрут через Gateway.
