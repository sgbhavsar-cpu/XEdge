# xEdge BACnet MS/TP daemon (Sprint P7, XEDGE-166). Links against the
# vendored bacnet-stack static library built with BACDL=mstp -- see
# third_party/README.md and docs/planning/license-audit.md §4 item 11 for
# why this is a separate linked daemon, not an in-process C extension.
BACNET_ROOT := $(realpath ../../../../third_party/bacnet-stack)
BACNET_SRC_DIR := $(BACNET_ROOT)/src
BACNET_PORT_DIR := $(BACNET_ROOT)/ports/linux
BACNET_POSIX_DIR := $(BACNET_ROOT)/ports/posix
BACNET_LIB_DIR := $(BACNET_ROOT)/apps/lib
BACNET_LIB := $(BACNET_LIB_DIR)/libbacnet.a

CC ?= gcc
CFLAGS += -Wall -Wextra -O2 -DBACDL_MSTP=1 \
	-I$(BACNET_SRC_DIR) -I$(BACNET_PORT_DIR) -I$(BACNET_POSIX_DIR)
LDLIBS := -lpthread -lm

TARGET := xedge-bacnet-mstp-daemon

# Client-side Device and Network Port BACnet object implementations --
# not part of libbacnet.a itself (that's app-supplied, same as every
# example app under third_party/bacnet-stack/apps/*/Makefile, e.g.
# apps/readprop/Makefile's own SRC list).
BACNET_OBJECT_DIR := $(BACNET_SRC_DIR)/bacnet/basic/object
EXTRA_SRCS := $(BACNET_OBJECT_DIR)/client/device-client.c \
	$(BACNET_OBJECT_DIR)/netport.c

.PHONY: all clean bacnet-lib

all: $(TARGET)

bacnet-lib:
	$(MAKE) -C $(BACNET_ROOT) BACDL=mstp lib

$(BACNET_LIB): bacnet-lib

$(TARGET): main.c $(EXTRA_SRCS) $(BACNET_LIB)
	$(CC) $(CFLAGS) -o $@ main.c $(EXTRA_SRCS) $(BACNET_LIB) $(LDLIBS)

clean:
	rm -f $(TARGET)
