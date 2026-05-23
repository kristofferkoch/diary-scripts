"""scripts/ as a Python package.

Previously this folder was a flat collection of standalone scripts and
`mail_reader/{extract,related}.py` imported from it via a sys.path
injection at module-import time — surprising and fragile. With this
empty `__init__.py` the folder is a proper package and consumers can
`from scripts.embed_mail import …` like any other module.

The standalone-script invocation pattern (`uv run scripts/foo.py`)
still works — Python doesn't require __init__.py for script execution.
"""
