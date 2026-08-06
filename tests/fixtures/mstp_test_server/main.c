/**
 * @file
 * @brief Deterministic MS/TP test-double server for xEdge's real-daemon
 * integration tests (Sprint P7, XEDGE-171) -- new xEdge test
 * infrastructure, not part of the vendored third_party/bacnet-stack (kept
 * pristine per docs/planning/license-audit.md §4 item 11's modification
 * policy).
 *
 * Deliberately NOT third_party/bacnet-stack/apps/server-mini: that example
 * app cycles AV-0/BV-0 through a fixed table of test values on a 5-second
 * timer (its own process_task(), which fires on the very first main-loop
 * iteration since last_update_time starts at 0) -- fine for interactive
 * prototyping, but it would make a permanent CI test race against that
 * timer. This server sets AV-0/BV-0 once at startup and never touches them
 * again, so every read across the whole test run is deterministic.
 *
 * Reuses server-mini's object-table wiring verbatim (same field order,
 * proven to compile/link against this exact pinned bacnet-stack version)
 * trimmed to only the object types this test double actually serves.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "bacnet/apdu.h"
#include "bacnet/bacdef.h"
#include "bacnet/bactext.h"
#include "bacnet/basic/binding/address.h"
#include "bacnet/basic/object/av.h"
#include "bacnet/basic/object/bv.h"
#include "bacnet/basic/object/device.h"
#include "bacnet/basic/services.h"
#include "bacnet/datalink/datalink.h"
#include "bacnet/datalink/dlenv.h"
#include "bacnet/npdu.h"

#include "bacnet/basic/service/h_apdu.h"
#include "bacnet/basic/service/h_rp.h"
#include "bacnet/basic/service/h_whois.h"
#include "bacnet/basic/service/h_wp.h"
#include "bacnet/basic/service/s_iam.h"

#define TEST_ANALOG_VALUE 85.3f
#define TEST_BINARY_ACTIVE 1

static uint8_t Rx_Buf[MAX_MPDU] = { 0 };
static uint32_t av_instance;
static uint32_t bv_instance;

static object_functions_t My_Object_Table[] = {
    { OBJECT_DEVICE,
      NULL,
      Device_Count,
      Device_Index_To_Instance,
      Device_Valid_Object_Instance_Number,
      Device_Object_Name,
      Device_Read_Property_Local,
      Device_Write_Property_Local,
      Device_Property_Lists,
      DeviceGetRRInfo,
      NULL,
      NULL,
      NULL,
      NULL,
      NULL,
      NULL,
      NULL,
      NULL,
      NULL,
      NULL,
      Device_Writable_Property_List },

    { OBJECT_ANALOG_VALUE,
      Analog_Value_Init,
      Analog_Value_Count,
      Analog_Value_Index_To_Instance,
      Analog_Value_Valid_Instance,
      Analog_Value_Object_Name,
      Analog_Value_Read_Property,
      NULL,
      Analog_Value_Property_Lists,
      NULL,
      NULL,
      Analog_Value_Encode_Value_List,
      NULL,
      NULL,
      NULL,
      NULL,
      NULL,
      Analog_Value_Create,
      Analog_Value_Delete,
      NULL,
      Analog_Value_Writable_Property_List },

    { OBJECT_BINARY_VALUE,
      Binary_Value_Init,
      Binary_Value_Count,
      Binary_Value_Index_To_Instance,
      Binary_Value_Valid_Instance,
      Binary_Value_Object_Name,
      Binary_Value_Read_Property,
      NULL,
      Binary_Value_Property_Lists,
      NULL,
      NULL,
      Binary_Value_Encode_Value_List,
      Binary_Value_Change_Of_Value,
      Binary_Value_Change_Of_Value_Clear,
      NULL,
      NULL,
      NULL,
      Binary_Value_Create,
      Binary_Value_Delete,
      NULL,
      Binary_Value_Writable_Property_List },

    { MAX_BACNET_OBJECT_TYPE,
      NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
      NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL }
};

static void Init_Service_Handlers(void)
{
    Device_Init(My_Object_Table);

    av_instance = Analog_Value_Create(0);
    Analog_Value_Name_Set(av_instance, "AV Read Only");
    Analog_Value_Present_Value_Set(av_instance, TEST_ANALOG_VALUE, BACNET_NO_PRIORITY);

    bv_instance = Binary_Value_Create(0);
    Binary_Value_Name_Set(bv_instance, "BV Read Only");
    Binary_Value_Present_Value_Set(bv_instance, TEST_BINARY_ACTIVE);

    apdu_set_unconfirmed_handler(SERVICE_UNCONFIRMED_WHO_IS, handler_who_is);
    apdu_set_confirmed_handler(
        SERVICE_CONFIRMED_READ_PROPERTY, handler_read_property);
    apdu_set_confirmed_handler(
        SERVICE_CONFIRMED_WRITE_PROPERTY, handler_write_property);
    apdu_set_unrecognized_service_handler_handler(handler_unrecognized_service);
}

static void print_usage(void)
{
    fprintf(
        stderr,
        "Usage: mstp_test_server --iface DEV --device-instance N "
        "[--mac M] [--baud B]\n");
}

int main(int argc, char *argv[])
{
    const char *iface = NULL;
    long device_instance = -1;
    long mac_address = 1;
    long baud_rate = 38400;
    int argi;
    BACNET_ADDRESS src = { 0 };
    uint16_t pdu_len;

    for (argi = 1; argi < argc; argi++) {
        if (strcmp(argv[argi], "--iface") == 0 && argi + 1 < argc) {
            iface = argv[++argi];
        } else if (strcmp(argv[argi], "--device-instance") == 0 && argi + 1 < argc) {
            device_instance = strtol(argv[++argi], NULL, 0);
        } else if (strcmp(argv[argi], "--mac") == 0 && argi + 1 < argc) {
            mac_address = strtol(argv[++argi], NULL, 0);
        } else if (strcmp(argv[argi], "--baud") == 0 && argi + 1 < argc) {
            baud_rate = strtol(argv[++argi], NULL, 0);
        } else {
            print_usage();
            return 1;
        }
    }
    if (!iface || device_instance < 0) {
        print_usage();
        return 1;
    }

    setenv("BACNET_IFACE", iface, 1);
    {
        char numbuf[32];
        snprintf(numbuf, sizeof(numbuf), "%ld", mac_address);
        setenv("BACNET_MSTP_MAC", numbuf, 1);
        snprintf(numbuf, sizeof(numbuf), "%ld", baud_rate);
        setenv("BACNET_MSTP_BAUD", numbuf, 1);
    }

    Device_Set_Object_Instance_Number((uint32_t)device_instance);
    dlenv_init();
    Init_Service_Handlers();
    atexit(datalink_cleanup);

    Send_I_Am(&Rx_Buf[0]);

    for (;;) {
        pdu_len = datalink_receive(&src, Rx_Buf, MAX_MPDU, 50);
        if (pdu_len) {
            npdu_handler(&src, Rx_Buf, pdu_len);
        }
    }

    return 0;
}
