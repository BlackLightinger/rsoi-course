.PHONY: install test run build validate-k8s dashboard-install dashboard-open

install:
	python3 -m pip install -e '.[test]'
	cd frontend && npm install

test:
	python3 -m compileall -q services
	pytest
	cd frontend && npm run build

run:
	docker compose up --build

build:
	docker compose build

validate-k8s:
	kubectl kustomize k8s/base >/dev/null

dashboard-install:
	./scripts/install-kubernetes-dashboard.sh

dashboard-open:
	./scripts/open-kubernetes-dashboard.sh
