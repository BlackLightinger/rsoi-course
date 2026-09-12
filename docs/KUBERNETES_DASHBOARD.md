# Kubernetes Dashboard

Проект содержит готовые файлы для установки Kubernetes Dashboard в локальный кластер Docker Desktop/Minikube.

## Установка

```bash
make dashboard-install
```

Команда применяет официальный Dashboard manifest и создаёт локального администратора:

- namespace `kubernetes-dashboard`;
- service account `admin-user`;
- `ClusterRoleBinding` на `cluster-admin`.

Это удобно для локального стенда и демонстрации. Для production такой широкий доступ лучше заменить на ограниченную роль.

## Открытие панели

```bash
make dashboard-open
```

Скрипт:

1. напечатает login token;
2. запустит `kubectl proxy`;
3. откроет Dashboard в браузере.

Если браузер не открылся автоматически, перейдите вручную:

```text
http://localhost:8001/api/v1/namespaces/kubernetes-dashboard/services/https:kubernetes-dashboard:/proxy/
```

В форме входа выберите `Token` и вставьте токен из терминала.

## Получить только token

```bash
./scripts/open-kubernetes-dashboard.sh --token-only
```

## Если порт 8001 занят

```bash
DASHBOARD_PROXY_PORT=8010 make dashboard-open
```
