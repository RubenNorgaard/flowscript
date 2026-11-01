# FlowScript C VM
#   make          build ./fsrun
#   make asan     build ./fsrun-asan (AddressSanitizer + UBSan)
#   make test     API unit test + conformance test against the Python VM
#   make fuzz     run the bytecode fuzzer against ./fsrun-asan
#   make size     size of flowscript.o built with -Os

CC      ?= cc
CFLAGS  ?= -std=c99 -Wall -Wextra -pedantic -O2 -ffp-contract=off
ASANFLAGS = -std=c99 -Wall -Wextra -pedantic -O1 -g -ffp-contract=off -fno-omit-frame-pointer \
            -fsanitize=address,undefined -fno-sanitize-recover=all
PYTHON  ?= python3

all: fsrun

fsrun: fsrun.c flowscript.c flowscript.h
	$(CC) $(CFLAGS) -Werror -o $@ fsrun.c flowscript.c -lm

fsrun-asan: fsrun.c flowscript.c flowscript.h
	$(CC) $(ASANFLAGS) -Werror -o $@ fsrun.c flowscript.c -lm

asan: fsrun-asan

flowscript.o: flowscript.c flowscript.h
	$(CC) -std=c99 -Wall -Wextra -pedantic -Werror -Os -ffp-contract=off -c -o $@ flowscript.c

size: flowscript.o
	size flowscript.o

test_api: test_api.c flowscript.c flowscript.h
	$(CC) $(ASANFLAGS) -Werror -o $@ test_api.c flowscript.c -lm

test: fsrun fsrun-asan test_api
	cd .. && $(PYTHON) -m flowscript compile tests/api/api.fs -o c/test_api.fsb > /dev/null
	./test_api test_api.fsb
	cd .. && $(PYTHON) tests/conformance.py

fuzz: fsrun-asan
	cd .. && $(PYTHON) tests/fuzz_c.py

clean:
	rm -f fsrun fsrun-asan test_api test_api.fsb flowscript.o
	rm -rf *.dSYM

.PHONY: all asan size test fuzz clean
