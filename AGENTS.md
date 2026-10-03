# Repository Guidelines

## Project Structure & Module Organization

`cloudflare/worker.js` is the hosted Telegram webhook and SQLite Durable Object implementation; `cloudflare/logic.js` holds shared parsing and date helpers. `cloudflare/worker.test.js` tests the hosted flow with Miniflare. `spendcue.py` is the separate, single-user Python polling bot, tested in `tests/test_spendcue.py`. `cloudflare/migrate.py` and `cloudflare/setup_webhook.py` support migration and setup. `wrangler.jsonc` configures the Worker. See `README.md` for user flows and deployment steps.

## Build, Test, and Development Commands

- `npm ci`: install the pinned Worker development dependencies.
- `npm test`: run Node's built-in test runner and Miniflare integration tests.
- `python3 -m unittest discover -s tests -v`: run the Python bot tests without credentials.
- `set -a; . ./.env; set +a; python3 spendcue.py`: run the local bot after filling `.env` from `.env.example`.
- `npx wrangler deploy --dry-run`: validate the Worker bundle without publishing it.

CI runs both test suites on pull requests and pushes to `main`. Production deployment is a separate **manual** GitHub Actions workflow (`Deploy SpendCue`, `workflow_dispatch` on `main`); pushing code does not update the live bot.

## Coding Style & Naming Conventions

Use four spaces in Python and two spaces in JavaScript. Follow existing `snake_case` Python names and `camelCase` JavaScript names. Keep money in integer minor units, validate dates and amounts before writes, and keep currency totals separate. Follow nearby code style; the repository has no configured formatter or linter.

## Testing Guidelines

Add focused `test_*` methods in `tests/test_spendcue.py` or `test(...)` cases in `cloudflare/worker.test.js` when behavior changes. Mock Telegram at the API boundary; tests must run without tokens or network access. Cover account isolation, duplicate updates, subscription schedules, card status, and reminder deduplication when touching those paths. Run both suites before opening a pull request.

## Commit & Pull Request Guidelines

Recent commits use short imperative subjects such as `Simplify subscription status to paid or unpaid`. Keep commits focused. Pull requests should describe user-visible behavior, note data or migration effects, and report both test results. Include Telegram screenshots when changing menus or button flows. Link an issue when one exists.

## Security & Configuration

Never commit `.env`, `.dev.vars`, SQLite files, backups, tokens, or full card numbers. Use the example environment files as templates. Preserve private-chat authorization and per-user Durable Object routing. Do not deploy as part of a routine code change; release only through the manual workflow when requested.
