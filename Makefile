PYTHON ?= python3
.PHONY: test demo check relation-demo
test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v
demo:
	PYTHONPATH=src $(PYTHON) examples/smoke.py
relation-demo:
	PYTHONPATH=src $(PYTHON) examples/relation_smoke.py
check: test demo relation-demo
