# Deterministic MS/TP test-double server (Sprint P7, XEDGE-171) -- new
# xEdge test infrastructure, built against the vendored bacnet-stack
# static library the same way xedge/drivers/bacnet/mstp_daemon builds
# against it. See main.c's own docstring for why this isn't
# third_party/bacnet-stack/apps/server-mini.
BACNET_ROOT := $(realpath ../../../third_party/bacnet-stack)
BACNET_SRC_DIR := $(BACNET_ROOT)/src
BACNET_PORT_DIR := $(BACNET_ROOT)/ports/linux
BACNET_POSIX_DIR := $(BACNET_ROOT)/ports/posix
BACNET_LIB_DIR := $(BACNET_ROOT)/apps/lib
BACNET_LIB := $(BACNET_LIB_DIR)/libbacnet.a

CC ?= gcc
CFLAGS += -Wall -Wextra -O2 -DBACDL_MSTP=1 \
	-I$(BACNET_SRC_DIR) -I$(BACNET_PORT_DIR) -I$(BACNET_POSIX_DIR)
LDLIBS := -lpthread -lm

TARGET := mstp-test-server

# device.c references every object type's function table at file scope
# (regardless of what My_Object_Table in main.c actually registers), so
# every object .c file needs linking too -- same full roster
# third_party/bacnet-stack/apps/server-mini/Makefile's own SRC list uses.
BACNET_OBJECT_DIR := $(BACNET_SRC_DIR)/bacnet/basic/object
EXTRA_SRCS := $(BACNET_OBJECT_DIR)/device.c \
	$(BACNET_OBJECT_DIR)/access_credential.c \
	$(BACNET_OBJECT_DIR)/access_door.c \
	$(BACNET_OBJECT_DIR)/access_point.c \
	$(BACNET_OBJECT_DIR)/access_rights.c \
	$(BACNET_OBJECT_DIR)/access_user.c \
	$(BACNET_OBJECT_DIR)/access_zone.c \
	$(BACNET_OBJECT_DIR)/ai.c \
	$(BACNET_OBJECT_DIR)/ao.c \
	$(BACNET_OBJECT_DIR)/av.c \
	$(BACNET_OBJECT_DIR)/acc.c \
	$(BACNET_OBJECT_DIR)/auditlog.c \
	$(BACNET_OBJECT_DIR)/bacfile.c \
	$(BACNET_OBJECT_DIR)/bi.c \
	$(BACNET_OBJECT_DIR)/bitstring_value.c \
	$(BACNET_OBJECT_DIR)/bo.c \
	$(BACNET_OBJECT_DIR)/blo.c \
	$(BACNET_OBJECT_DIR)/bv.c \
	$(BACNET_OBJECT_DIR)/calendar.c \
	$(BACNET_OBJECT_DIR)/channel.c \
	$(BACNET_OBJECT_DIR)/color_object.c \
	$(BACNET_OBJECT_DIR)/color_temperature.c \
	$(BACNET_OBJECT_DIR)/command.c \
	$(BACNET_OBJECT_DIR)/credential_data_input.c \
	$(BACNET_OBJECT_DIR)/csv.c \
	$(BACNET_OBJECT_DIR)/iv.c \
	$(BACNET_OBJECT_DIR)/lc.c \
	$(BACNET_OBJECT_DIR)/lo.c \
	$(BACNET_OBJECT_DIR)/loop.c \
	$(BACNET_OBJECT_DIR)/lsp.c \
	$(BACNET_OBJECT_DIR)/lsz.c \
	$(BACNET_OBJECT_DIR)/ms-input.c \
	$(BACNET_OBJECT_DIR)/mso.c \
	$(BACNET_OBJECT_DIR)/msv.c \
	$(BACNET_OBJECT_DIR)/nc.c \
	$(BACNET_OBJECT_DIR)/netport.c \
	$(BACNET_OBJECT_DIR)/osv.c \
	$(BACNET_OBJECT_DIR)/piv.c \
	$(BACNET_OBJECT_DIR)/program.c \
	$(BACNET_OBJECT_DIR)/schedule.c \
	$(BACNET_OBJECT_DIR)/structured_view.c \
	$(BACNET_OBJECT_DIR)/time_value.c \
	$(BACNET_OBJECT_DIR)/timer.c \
	$(BACNET_OBJECT_DIR)/trendlog.c

.PHONY: all clean bacnet-lib

all: $(TARGET)

bacnet-lib:
	$(MAKE) -C $(BACNET_ROOT) BACDL=mstp lib

$(BACNET_LIB): bacnet-lib

$(TARGET): main.c $(EXTRA_SRCS) $(BACNET_LIB)
	$(CC) $(CFLAGS) -o $@ main.c $(EXTRA_SRCS) $(BACNET_LIB) $(LDLIBS)

clean:
	rm -f $(TARGET)
