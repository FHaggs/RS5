WORK_DIR     ?= .
BRANCHPRED   ?= 1
FORWARDING   ?= 1
DUALPORT_MEM ?= 1
IQUEUE_SIZE  ?= 2
DELAY_CYCLES ?= 0
TRIPLET      ?= riscv64-elf

# Fuzzer: FUZZ_CONFIG selects rs5/<FUZZ_CONFIG>.yaml and <FUZZ_CONFIG>.ini
FUZZ_CONFIG  ?= baseline
FUZZ_ARGS    ?=

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

extensions: arch-test-update
	@$(ENV_VARS) riscof run --suite=riscv-arch-test/riscv-test-suite --env=riscv-arch-test/riscv-test-suite/env --work-dir=$(WORK_DIR)/extensions_work --no-browser --config=extensions.ini

baseline: arch-test-update
	@$(ENV_VARS) riscof run --suite=riscv-arch-test/riscv-test-suite --env=riscv-arch-test/riscv-test-suite/env --work-dir=$(WORK_DIR)/baseline_work --no-browser --config=baseline.ini

arch-test-update: riscv-arch-test
	@riscof arch-test --update --get-version 3.9.1

riscv-arch-test:
	@riscof arch-test --clone --get-version 3.9.1

# Same as baseline/extensions, with a coverage-instrumented model and a coverage report
coverage-baseline coverage-extensions: coverage-%: arch-test-update
	@$(ENV_VARS) RS5_COVERAGE=$(or $(RS5_COVERAGE),1) riscof run --suite=riscv-arch-test/riscv-test-suite --env=riscv-arch-test/riscv-test-suite/env --work-dir=$(WORK_DIR)/$*_cov_work --no-browser --config=$*.ini
	@python3 fuzzer/coverage_report.py $(WORK_DIR)/$*_cov_work

fuzz: arch-test-update
	@$(ENV_VARS) python3 fuzzer/fuzz.py --isa-yaml rs5/$(FUZZ_CONFIG).yaml --work-dir $(WORK_DIR)/fuzz_work $(FUZZ_ARGS)

fuzz-riscof:
	@$(ENV_VARS) riscof run --suite=$(WORK_DIR)/fuzz_work/suite --env=riscv-arch-test/riscv-test-suite/env --work-dir=$(WORK_DIR)/fuzz_riscof_work --no-browser --config=$(FUZZ_CONFIG).ini

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
	@rm -rf $(WORK_DIR)/baseline_work $(WORK_DIR)/extensions_work $(WORK_DIR)/fuzz_work $(WORK_DIR)/fuzz_riscof_work $(WORK_DIR)/*_cov_work riscv-arch-test

.PHONY: all default baseline extensions arch-test-update fuzz fuzz-riscof coverage-baseline coverage-extensions clean
