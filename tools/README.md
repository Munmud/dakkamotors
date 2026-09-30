# tools/

Three scripts for looking at the site rather than testing it. They are not part of the
suite and CI does not run them: they need a browser, a seeded table and two servers, and
what they produce is something a person reads.

They live here because the alternative is a folder of one-off scripts in a chat log, and
because two of the three found real bugs the test suite could not have — a contrast
ratio and a line break are not assertions anybody thinks to write.

```bash
# 1. A lot to look at, in a scratch table.
docker compose up -d dynamodb          # or: python -m moto.server -p 8123
cd backend && source .venv/Scripts/activate
export DJANGO_SETTINGS_MODULE=config.settings SECRET_KEY=dev-only-insecure-key
export DYNAMODB_ENDPOINT_URL=http://127.0.0.1:8123 DDB_TABLE=dakkamotors_shots
export AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local
export AWS_DEFAULT_REGION=ap-northeast-1 PYTHONPATH=$PWD
PHOTOS=/path/to/some/car/photos python ../tools/seed_lot.py

# 2. Both servers. Vite proxies /api to Django, the way CloudFront does in production.
python manage.py runserver 127.0.0.1:8000 --noreload &
cd ../frontend && npm run dev &

# 3. The passes.
python ../tools/screenshot.py ../docs/screenshots
python ../tools/contrast.py
```

## screenshot.py

Every public page at 390px and 1440px, in Japanese and English, dark. Plus the two
states a visitor only reaches by acting: the form filled in, and the confirmation that
replaces it.

It scrolls each page before capturing. `full_page=True` stitches a tall image without
ever moving the viewport, so every `loading="lazy"` photograph below the fold stays
unfetched and photographs as an empty box — an artefact of the harness that makes the
output lie about the thing it exists to show.

**It submits a real lead at the end.** Point it at a local server.

## contrast.py

WCAG AA over every rendered text node, walking up the tree for the first opaque
background because text almost never sits on an element that paints one.

This exists because a dark theme fails *quietly*. Dim grey on near-black looks
deliberate at any ratio. When the site was inverted, `--text` went from near-black to
near-white and four rules that paired it with a light fill inverted with it and said
nothing: the price — the single number the page is for — rendered white on the yellow
plate at **1.51:1**, and the back link at 2.03:1. Nothing was broken, nothing logged,
and both are obvious once a number is put on them.

## seed_lot.py

Four cars: a full spec sheet, a reserved one, one with no price, one with a single
photo. Between them that is every branch the listing and detail templates have.
