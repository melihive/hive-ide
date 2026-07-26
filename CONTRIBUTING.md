# Contributing

`hive-ide` requires Python 3.10+, `tmux`, and a Unix-like system.

```sh
python3 -m venv .venv-dev
.venv-dev/bin/python -m pip install -e ".[test,release]"
.venv-dev/bin/python -m pytest
.venv-dev/bin/python -m build
.venv-dev/bin/python -m twine check dist/*
```

Keep the standalone package host-neutral. Host-specific workspace, plan, and command
behavior belongs behind adapters or plugin entry points.
