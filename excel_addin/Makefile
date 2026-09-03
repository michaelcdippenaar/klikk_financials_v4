# Checks for the Klikk Journals add-in. `make check` is the whole gate.
#
#   make check   lint + boot/build/pure tests          (run before every ship)
#   make prove   reconstruct the 2026-09-03 bugs and show each check catch one
#
# Needs `npm ci` once (eslint + jsdom; nothing else, no bundler).
.PHONY: check lint test prove install

check: lint test

install:
	npm ci

lint:
	npm run lint

test:
	npm test

prove:
	npm run prove
