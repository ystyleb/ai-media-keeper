.PHONY: tailwind-install tailwind-watch tailwind-build dev test

tailwind-install:
	@./scripts/install_tailwind.sh

tailwind-watch: tailwind-install
	@./bin/tailwindcss -i static/css/input.css -o static/css/output.css --watch

tailwind-build: tailwind-install
	@./bin/tailwindcss -i static/css/input.css -o static/css/output.css --minify

dev: tailwind-build
	@flask --app app run --debug --port 5001

test:
	@pytest tests/ -v
