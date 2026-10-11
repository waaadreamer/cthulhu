# Repository instructions

- Authority, routing and context rules live in code (`cthulhu/roles.py`, `temple.py`), never in skills.
- Skills in `cthulhu/roles/` only pass the owner's will down; keep them short.
- Never commit credentials, configs from `/etc/cthulhu`, databases or conversation logs.
- Run `venv/bin/python -m unittest discover -s tests -v` before publishing changes.
- After editing `web/src`, run `npm run build` in `web/` and commit `cthulhu/static`.
