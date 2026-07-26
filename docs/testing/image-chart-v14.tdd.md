# Image Chart V14 TDD Evidence

Source: current image-chart repair handoff.

| Guarantee | Test command | Result |
| --- | --- | --- |
| Image sessions do not cross chats for the same user | `pytest -q -p no:cacheprovider --basetemp=<temp> tests/mingli_console/test_image_chart_intake.py -k same_user` | RED then GREEN: 1 passed |
| Confirmed image dispatches the Runtime once and cancel removes its session | `pytest -q -p no:cacheprovider --basetemp=<temp> tests/mingli_console/test_image_chart_intake.py -k 'confirmed_image_dispatches or cancel_clears'` | RED then GREEN: 2 passed |
| Image confirmation flow and existing focused image cases | `pytest -q -p no:cacheprovider --basetemp=<temp> tests/mingli_console/test_image_chart_intake.py` | 11 passed |

The Telegram adapter suite was not run locally because the available isolated Python environment lacks `requests` and `python-telegram-bot`. The generic console suite reaches its assertions but is reported failed on this Windows host when temporary SQLite files remain locked during cleanup.
