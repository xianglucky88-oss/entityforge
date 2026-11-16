PYTHON ?= python3
ROOT := $(CURDIR)
VENDOR := $(ROOT)/vendor/wheels
RELENG := $(ROOT)/releng
WORK := $(ROOT)/release-work
RELEASE := $(ROOT)/release

.PHONY: test demo check vendor-manifest release-build release-repro \
	release-accept release publish release-test clean-release

# --- existing development entry points (unchanged black-box behavior) -------
test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

demo:
	PYTHONPATH=src $(PYTHON) examples/smoke.py

check: test demo

# --- offline release chain --------------------------------------------------
vendor-manifest:
	$(PYTHON) vendor/write-manifest.py

# Full chain for the working tree: freeze manifest, raw + normalized sdist
# and wheel, sdist-rebuilt wheel, isolated acceptance (source chmod 000).
release-build:
	$(PYTHON) -I $(RELENG)/build_release.py build \
		--source $(ROOT) --work-dir $(WORK)/build --vendor $(VENDOR) \
		--report $(WORK)/release-report.json

# Two absolute paths, two source mtimes, raw digests + reproducibility verdict.
release-repro:
	$(PYTHON) -I $(RELENG)/repro_helper.py $(WORK)/repro

# Acceptance alone, against an already built wheel.
release-accept:
	$(PYTHON) -I $(RELENG)/acceptance.py \
		$(WORK)/build/2-normalized/entityforge-0.1.0-py3-none-any.whl \
		--source-root $(ROOT) --work-dir $(WORK)/accept \
		--vendor $(VENDOR) --report $(WORK)/acceptance-report.json

release: release-build
	$(PYTHON) -I $(RELENG)/publish.py $(RELEASE) $(WORK)/release-report.json

# Full proof suite: races, tamper hooks, pre-commit exit, disk failure,
# offline enforcement.
release-test:
	$(PYTHON) -I $(RELENG)/test_release.py

clean-release:
	rm -rf $(WORK) $(RELEASE)
