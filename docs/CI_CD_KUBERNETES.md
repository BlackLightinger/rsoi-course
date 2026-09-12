# CI/CD и Kubernetes deployment

## Что собирается

GitHub Actions workflow `.github/workflows/ci-cd.yml` обслуживает весь прототип:

- `idp` — Identity Provider и пользовательские данные;
- `flights` — рейсы, города и места;
- `tickets` — бронирования, пассажиры, багаж и онлайн-регистрация;
- `loyalty` — бонусная программа;
- `payments` — sandbox-оплаты;
- `statistics` — Kafka consumer и отчёты;
- `gateway` — BFF, агрегация и partner API;
- `web` — React/Vite UI под NGINX.

Kafka в Kubernetes используется как инфраструктурная зависимость из официального образа `apache/kafka`.

## Pipeline

На pull request workflow выполняет проверки:

1. устанавливает Python-зависимости;
2. компилирует Python-код;
3. запускает тесты;
4. собирает frontend;
5. проверяет Kubernetes-манифесты через `kubectl kustomize k8s/base`.

На push в `main` после успешных проверок:

1. для каждого Python-сервиса собирается отдельный Docker image из `docker/python-service.Dockerfile`;
2. для `web` собирается отдельный NGINX image из `frontend/Dockerfile`;
3. images публикуются в GHCR с тегами `${GITHUB_SHA}` и `latest`;
4. workflow подключается к Kubernetes-кластеру через `KUBE_CONFIG`;
5. применяет манифесты `k8s/base`;
6. обновляет runtime secrets;
7. выставляет каждому deployment immutable image `ghcr.io/<owner>/<repo>/<service>:<sha>`;
8. ждёт rollout для Kafka StatefulSet и всех application deployment’ов.

## Требования к кластеру

В кластере должны быть:

- рабочий `kubectl`-доступ у CI;
- default `StorageClass` для PVC;
- установленный NGINX Ingress Controller с `ingressClassName: nginx`;
- namespace `ingress-nginx` с label `kubernetes.io/metadata.name=ingress-nginx` — это стандартно для современных Kubernetes namespace и используется NetworkPolicy.

Пример установки NGINX Ingress Controller:

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --create-namespace
```

## Secrets и переменные GitHub

Для production environment в GitHub нужно задать secrets:

- `KUBE_CONFIG` — kubeconfig для целевого кластера;
- `ADMIN_PASSWORD` — пароль администратора IdP;
- `GATEWAY_CLIENT_SECRET` — client secret сервисного клиента Gateway;
- `PARTNER_API_KEY` — ключ ограниченного partner API.

И environment variable:

- `PUBLIC_URL` — внешний адрес приложения, например `https://flight.example.com`.

`PUBLIC_URL` используется как OIDC issuer и redirect URI, поэтому он должен совпадать с host, который ведёт на Ingress.

## Kubernetes-манифесты

`k8s/base` содержит:

- `Namespace`;
- `ConfigMap` и placeholder `Secret`;
- `StatefulSet` + headless `Service` для Kafka;
- PVC для stateful-доменных сервисов;
- `Deployment` и `Service` для каждого application service;
- `Ingress` с маршрутами:
  - `/` → `web`;
  - `/api`, `/partner` → `gateway`;
  - `/oauth2`, `/.well-known` → `idp`;
- `NetworkPolicy`, закрывающие прямой вход в доменные сервисы и оставляющие доступ через Gateway/Ingress.

Локальная проверка манифестов:

```bash
kubectl kustomize k8s/base >/dev/null
```

Ручное применение для разработки:

```bash
kubectl apply -k k8s/base
```

В production основной путь — через GitHub Actions, потому что CI/CD заменяет placeholder images на immutable images конкретного commit SHA.
