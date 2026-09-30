WORK_DIR     ?= .
BRANCHPRED   ?= 1
FORWARDING   ?= 1
DUALPORT_MEM ?= 1
IQUEUE_SIZE  ?= 2
DELAY_CYCLES ?= 0
TRIPLET      ?= riscv64-elf
PYTHON       ?= python3

ENV_VARS = \
	TRIPLET=$(TRIPLET)\
	BRANCHPRED=$(BRANCHPRED)\
	FORWARDING=$(FORWARDING)\
	DUALPORT_MEM=$(DUALPORT_MEM)\
	IQUEUE_SIZE=$(IQUEUE_SIZE) \
	DELAY_CYCLES=$(DELAY_CYCLES)

default: baseline

all: baseline extensions

report-all: report-baseline report-extensions

fuzzer-demo:
	@$(ENV_VARS) $(PYTHON) fuzzer_demo.py

fuzzer-generate:
	@$(PYTHON) fuzzer_demo.py --generate-only

extensions: arch-test-update
	@$(ENV_VARS) riscof run --suite=riscv-arch-test/riscv-test-suite --env=riscv-arch-test/riscv-test-suite/env --work-dir=$(WORK_DIR)/extensions_work --no-browser --config=extensions.ini

baseline: arch-test-update
	@$(ENV_VARS) riscof run --suite=riscv-arch-test/riscv-test-suite --env=riscv-arch-test/riscv-test-suite/env --work-dir=$(WORK_DIR)/baseline_work --no-browser --config=baseline.ini

arch-test-update: riscv-arch-test
	@riscof arch-test --update --get-version 3.9.1

riscv-arch-test:
	@riscof arch-test --clone --get-version 3.9.1

report-baseline: $(WORK_DIR)/baseline_work/report.html
	@xdg-open $^

report-extensions: $(WORK_DIR)/extensions_work/report.html
	@xdg-open $^

$(WORK_DIR)/baseline_work/report.html:
	@make baseline

$(WORK_DIR)/extensions_work/report.html:
	@make extensions

clean:
	@echo "Cleaning..."
	@rm -rf $(WORK_DIR)/baseline_work $(WORK_DIR)/extensions_work fuzzer_demo_suite fuzzer_demo_work riscv-arch-test

.PHONY: all default baseline extensions fuzzer-demo fuzzer-generate arch-test-update clean
