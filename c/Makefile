# FlowScript C VM
#   make          build ./fsrun
#   make asan     build ./fsrun-asan (AddressSanitizer + UBSan)
#   make test     API unit test + conformance test against the Python VM
#   make fuzz     run the bytecode fuzzer against ./fsrun-asan
#   make size     size of flowscript.o built with -Os
#   make switches check that every FS_NO_FLOAT / FS_NO_TIME / FS_NO_STR combination compiles

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

# Reduced builds (feature switches), sanitized: the conformance test runs every
# program on each one whose switches it allows, and checks the others refuse it.
fsrun-nofloat: fsrun.c flowscript.c flowscript.h
	$(CC) $(ASANFLAGS) -DFS_NO_FLOAT -Werror -o $@ fsrun.c flowscript.c

fsrun-notime-nostr: fsrun.c flowscript.c flowscript.h
	$(CC) $(ASANFLAGS) -DFS_NO_TIME -DFS_NO_STR -Werror -o $@ fsrun.c flowscript.c -lm

fsrun-min: fsrun.c flowscript.c flowscript.h
	$(CC) $(ASANFLAGS) -DFS_NO_FLOAT -DFS_NO_TIME -DFS_NO_STR -Werror -o $@ fsrun.c flowscript.c

VARIANTS = fsrun-nofloat fsrun-notime-nostr fsrun-min

# every combination of the switches must compile without warnings
switches:
	@for f in "" "-DFS_NO_FLOAT" "-DFS_NO_TIME" "-DFS_NO_STR" "-DFS_NO_FLOAT -DFS_NO_TIME" \
	          "-DFS_NO_FLOAT -DFS_NO_STR" "-DFS_NO_TIME -DFS_NO_STR" \
	          "-DFS_NO_FLOAT -DFS_NO_TIME -DFS_NO_STR"; do \
	  $(CC) -std=c99 -Wall -Wextra -pedantic -Werror -O2 -ffp-contract=off $$f -c -o /dev/null flowscript.c || exit 1; \
	  $(CC) -std=c99 -Wall -Wextra -pedantic -Werror -O2 -ffp-contract=off $$f -c -o /dev/null fsrun.c || exit 1; \
	done; echo "switches: all 8 combinations compile"

flowscript.o: flowscript.c flowscript.h
	$(CC) -std=c99 -Wall -Wextra -pedantic -Werror -Os -ffp-contract=off -c -o $@ flowscript.c

size: flowscript.o
	size flowscript.o

test_api: test_api.c flowscript.c flowscript.h
	$(CC) $(ASANFLAGS) -Werror -o $@ test_api.c flowscript.c -lm

test: fsrun fsrun-asan test_api switches $(VARIANTS)
	cd .. && $(PYTHON) -m flowscript compile tests/api/api.fs -o c/test_api.fsb > /dev/null
	./test_api test_api.fsb
	cd .. && $(PYTHON) tests/conformance.py

fuzz: fsrun-asan
	cd .. && $(PYTHON) tests/fuzz_c.py

clean:
	rm -f fsrun fsrun-asan test_api test_api.fsb flowscript.o $(VARIANTS)
	rm -rf *.dSYM

.PHONY: all asan size test fuzz clean switches
