# Source preparation only. Installation/UID selection requires operator approval.
# Static linkage is mandatory: a dynamic launcher's loader executes before main.
CC = /usr/bin/cc
CFLAGS = -O2 -Wall -Wextra -Werror -fstack-protector-strong
LDFLAGS = -static
PYTHON_MINOR ?= 12

.PHONY: all clean FORCE
all: elspeth-operator

elspeth-operator: launcher.c FORCE
	$(CC) $(CPPFLAGS) -DELSPETH_PYTHON_MINOR=$(PYTHON_MINOR) $(CFLAGS) $(LDFLAGS) -o $@ $<

clean:
	rm -f elspeth-operator

FORCE:
