import os
import re
import subprocess
import logging
from multiprocessing import cpu_count

import riscof.utils as utils
from riscof.pluginTemplate import pluginTemplate

from rs5_verilator import coverage_args_from_env, verilator_cmd

logger = logging.getLogger()

class rs5(pluginTemplate):
    __model__ = "RS5"
    __version__ = "XXX"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        config = kwargs.get('config')

        # At minimum we need the paths to the ispec and pspec files
        if config is None:
            print("Please enter input file paths in configuration.")
            raise SystemExit(1)

        # Number of parallel jobs that can be spawned off by RISCOF
        self.num_jobs = str(cpu_count())

        # Path to the directory where this python file is located
        self.pluginpath=os.path.abspath(config['pluginpath'])

        # Collect the paths to the riscv-config absed ISA and platform yaml files
        self.isa_spec = os.path.abspath(config['ispec'])
        self.platform_spec = os.path.abspath(config['pspec'])

        self.triplet = os.environ["TRIPLET"]

        # If you are interested in just compiling the tests and not running
        if 'target_run' in config and config['target_run']=='0':
            self.target_run = False
        else:
            self.target_run = True

    def initialise(self, suite, work_dir, archtest_env):
        # capture the working directory
        self.work_dir = work_dir

        # capture the architectural test-suite directory.
        self.suite_dir = suite

        # Note the march is not hardwired here, because it will change for each test
        self.compile_cmd = self.triplet + '-gcc -march={0} \
            -static -mcmodel=medany -fvisibility=hidden -nostdlib -nostartfiles -g\
            -T '+self.pluginpath+'/env/link.ld\
            -I '+self.pluginpath+'/env/\
            -I ' + archtest_env + ' {2} -o {3} {4}'
        
        # the testbench is verilated once in build()
        self.obj_dir = os.path.join(self.work_dir, 'obj_dir')

    def build(self, isa_yaml, platform_yaml):
        # load the isa yaml as a dictionary in python.
        ispec = utils.load_yaml(isa_yaml)['hart0']

        # capture the XLEN value by picking the max value in 'supported_xlen' field of isa yaml
        self.xlen = ('64' if 64 in ispec['supported_xlen'] else '32')

        # '--isa' argument
        self.isa = 'rv' + self.xlen
        # RS5_COVERAGE=1 builds a coverage-instrumented model: each test then leaves a
        # coverage.dat in its dut/ directory (see fuzzer/coverage_report.py)
        self.verilatecmd = verilator_cmd(ispec["ISA"], self.obj_dir, coverage_args_from_env())

        self.compile_cmd = self.compile_cmd+' -mabi='+('lp64 ' if 64 in ispec['supported_xlen'] else 'ilp32 ')

        # compile the testbench once; per-test values are passed as plusargs
        if self.target_run:
            logger.info("Verilating riscof_tb: " + " ".join(self.verilatecmd))
            subprocess.run(self.verilatecmd, check=True)

    def runTests(self, testList):
        # Delete Makefile if it already exists.
        if os.path.exists(self.work_dir+ "/Makefile." + self.name[:-1]):
            os.remove(self.work_dir+ "/Makefile." + self.name[:-1])
        
        # create an instance the makeUtil class that we will use to create targets.
        make = utils.makeUtil(makefilePath=os.path.join(self.work_dir, "Makefile." + self.name[:-1]))

        # set the make command that will be used
        make.makeCommand = 'make -k -j' + self.num_jobs

        # we will iterate over each entry in the testList
        for testname in testList:
            # for each testname we get all its fields (as described by the testList format)
            testentry = testList[testname]

            # we capture the path to the assembly file of this test
            test = testentry['test_path']

            # capture the directory where the artifacts of this test
            test_dir = testentry['work_dir']

            # name of the elf file after compilation of the test
            elf = os.path.join(test_dir,"my.elf")

            # name of the signature file
            sig_file = os.path.join(test_dir, self.name[:-1] + ".signature")

            # for each test there are specific compile macros that need to be enabled
            compile_macros= ' -D' + " -D".join(testentry['macros'])

            # substitute all variables in the compile command
            cmd = self.compile_cmd.format(testentry['isa'].lower(), self.xlen, test, elf, compile_macros)

            if self.target_run:
                subprocess.run(cmd.split())
                symbols = subprocess.check_output([self.triplet+"-readelf", elf, "-s"]).split(b'\n')[3:]

                tohost_addr = ""
                signature_start = ""
                signature_end = ""

                for line in symbols[:-1]: 
                    l = line.split()
                    addr = l[1]
                    sym = l[-1]

                    if sym == b'tohost':
                        tohost_addr = addr.decode('utf-8')
                    elif sym == b'begin_signature':
                        signature_start = addr.decode('utf-8')
                    elif sym == b'end_signature':
                        signature_end = addr.decode('utf-8')

                # generate .bin
                objcopycmd = f'{self.triplet}-objcopy {elf} test.bin -O binary' 

                simcmd = f'{self.obj_dir}/Vriscof_tb\
                    +SIG_START={signature_start}\
                    +SIG_END={signature_end}\
                    +TOHOST_ADDR={tohost_addr}\
                    +SIG_PATH={sig_file}\
                    +META_PATH=meta.json'
            else:
                simcmd = 'echo "NO RUN"'
                objcopycmd = ''

            # concatenate all commands that need to be executed within a make-target.
            execute = 'cd {}; {}; {}; {};'.format(testentry['work_dir'], cmd, objcopycmd, simcmd)

            # create a target
            make.add_target(execute)

        # run all the targets in parallel using the make command set above
        make.execute_all(self.work_dir)

        # if target runs are not required then we simply exit
        if not self.target_run:
            raise SystemExit(0)
