# Contributing

## Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cd ..
ruff check .
pytest
```

- Keep `config.py` the single source of tunables; expose new thresholds through `.env`.
- Vision changes must keep every case in `backend/tools/probe_vision.py` passing on both the
  OpenCV and NumPy paths (`pytest backend/tests/test_vision.py`). If you recalibrate against real
  frames, record how in the comment next to the constant.
- The verdict status must stay deterministic. The language model may phrase it, never decide it.

## Firmware

- Build with ESP-IDF v5.4+: `cd firmware/esp-idf && idf.py build`.
- Never commit `isms_secrets.h` or real credentials.
- Leave `components/esp_bsp/bsp_display.c` untouched.

## Pull requests

- One logical change per PR, with a short description of what changed and how you verified it
  (test output, serial log, or HMI screenshot).
- CI must pass: lint, tests, and the firmware build.
