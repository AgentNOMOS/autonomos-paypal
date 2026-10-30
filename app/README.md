# AutoNOMOS application

See the repository root `README.md` for what this is, how to run it, and its limits.

```bash
python3 -m unittest discover -s tests                 # 346 tests, offline
python3 -m autonomos demo /tmp/autonomos-demo --ai recorded
python3 -m autonomos                                  # list every command
```

Python standard library only (Python 3.10+, tested on 3.12). The tests expect the owner launcher at `../live_demo/`.
