# Source preparation only. Installation/UID selection requires operator approval.
# Static linkage is mandatory: a dynamic launcher's loader executes before main.
CC = /usr/bin/cc
CFLAGS = -O2 -Wall -Wextra -Werror -fstack-protector-strong
LDFLAGS = -static

.PHONY: all clean
all: elspeth-operator

elspeth-operator: launcher.c
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $<

clean:
	rm -f elspeth-operator
