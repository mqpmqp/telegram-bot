# Image Chart V14 TDD Evidence

Source: current image-chart repair handoff.

| Guarantee | Test command | Result |
| --- | --- | --- |
| Image sessions do not cross chats for the same user | `pytest -q -p no:cacheprovider --basetemp=<temp> tests/mingli_console/test_image_chart_intake.py -k same_user` | RED then GREEN: 1 passed |
| Confirmed image dispatches the Runtime once and cancel removes its session | `pytest -q -p no:cacheprovider --basetemp=<temp> tests/mingli_console/test_image_chart_intake.py -k 'confirmed_image_dispatches or cancel_clears'` | RED then GREEN: 2 passed |
| Image confirmation flow and existing focused image cases | `pytest -q -p no:cacheprovider --basetemp=<temp> tests/mingli_console/test_image_chart_intake.py` | 11 passed |

## V14.1 Production Reliability Gate

The isolated test environment was synchronized with:

```text
uv sync --frozen --extra messaging --extra dev --no-install-project
```

No lock file or project dependency declaration was changed.

| Guarantee | Test command | Result |
| --- | --- | --- |
| Persistent event, image-hash, session, Runtime claim, and audit storage | `pytest -q -p no:cacheprovider --basetemp=<temp> tests/mingli_console/test_image_reliability_store.py tests/mingli_console/test_image_chart_intake.py -k 'reliability or restart'` | RED: missing `mingli_console.image_reliability_store`; GREEN: 3 passed |
| Restart-safe confirmation, Runtime idempotency, terminal audit preservation, and completion-write fallback | `pytest -q -p no:cacheprovider --basetemp=<temp> tests/mingli_console/test_image_reliability_store.py tests/mingli_console/test_image_chart_intake.py` | 18 passed |
| Telegram `update_id`, `(chat_id, message_id)`, image-hash, and confirmation dedupe | `pytest -q -p no:cacheprovider --basetemp=<temp> tests/mingli_console/test_telegram_image_chart_handler.py -k 'same_update_id or same_message_id or same_image_hash or duplicate_confirmation_event'` | RED: 3 failed, 1 passed; GREEN: 4 passed |
| Provider failure releases the image-hash claim for an explicit retry | `pytest -q -p no:cacheprovider --basetemp=<temp> tests/mingli_console/test_telegram_image_chart_handler.py -k retry_after_provider_failure` | RED then GREEN: 1 passed |
| Telegram adapter image routing and audit integration | `pytest -q -p no:cacheprovider --basetemp=<temp> tests/mingli_console/test_telegram_image_chart_handler.py` | 21 passed, 19 subtests passed |
| MingLi console regression suite | `pytest -q -p no:cacheprovider --basetemp=<temp> tests/mingli_console` | 49 passed, 19 subtests passed |
| Affected Telegram gateway regression | `pytest -q -p no:cacheprovider --basetemp=<temp> tests/gateway/test_telegram_{channel_posts,auth_check,bot_auth_bypass,photo_interrupts,text_batching}.py` | 43 passed |
| Reliability store branch coverage | `coverage run --branch -m pytest ...` then `coverage report --include='mingli_console/image_reliability_store.py' --fail-under=80` | 100% |
| Syntax and style | `python -m compileall -q mingli_console plugins/platforms/telegram tests/mingli_console`; `ruff check ...`; `git diff --check` | PASS |
