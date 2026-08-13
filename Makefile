# Convenience wrappers around docker compose. On Windows without `make`, run the commands directly.
.PHONY: up down build logs test create-event admit

up:            ## build + start the whole system (http://localhost:8080)
	docker compose up --build -d

down:          ## stop and remove containers (keeps the postgres volume)
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f

test:          ## run both test suites inside the images
	docker compose run --rm queue python manage.py test
	docker compose run --rm booking python manage.py test bookings

# Operational, per-event (an event exists once its Redis config hash does):
create-event:  ## make create-event EVENT=<slug> RATE=<n> BURST=<n> BATCH=<n>
	docker compose exec queue python manage.py create_event $(EVENT) --rate-per-min $(RATE) --burst $(BURST) --batch-max $(BATCH)

admit:         ## make admit EVENT=<slug> — run an admission loop for one event
	docker compose exec queue python manage.py run_admitter $(EVENT)
