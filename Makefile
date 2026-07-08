.PHONY: up down build migrate makemigrations test lint format shell logs

up:
	docker compose up

build:
	docker compose build

down:
	docker compose down

migrate:
	docker compose run --rm backend python manage.py migrate

makemigrations:
	docker compose run --rm backend python manage.py makemigrations

test:
	docker compose run --rm backend pytest

lint:
	ruff check .

format:
	ruff format .

shell:
	docker compose run --rm backend python manage.py shell

logs:
	docker compose logs -f
