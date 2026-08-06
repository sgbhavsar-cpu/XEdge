/**
 * @file
 * @brief xEdge BACnet MS/TP daemon (Sprint P7, XEDGE-166) -- new code, not
 * part of bacnet-stack. Links bacnet-stack directly (permitted by its
 * GCC-exception-2.0 license on the core files this links against; see
 * docs/planning/license-audit.md §4 item 11) as an MS/TP master, and
 * exposes a minimal ReadProperty-only client over a local Unix domain
 * socket for xEdge's Python driver to talk to. One daemon instance per
 * RS-485 port; the daemon owns the whole MS/TP token-passing lifecycle so
 * the Python side never blocks on it directly (Sprint P7 architecture
 * decision -- daemon-per-port over IPC, not an in-process C binding).
 *
 * Wire protocol (newline-delimited JSON over the Unix socket, one request
 * in flight at a time -- matches the one Python driver instance that owns
 * this daemon 1:1):
 *   -> {"device_instance":N,"mac":M,"object_type":"analog-input",
 *       "object_instance":I,"property_id":"present-value"}
 *   <- {"ok":true,"value":<number|bool|string>}
 *   <- {"ok":false,"error":"<reason>"}
 *
 * Deliberately minimal: no WhoIs/I-Am discovery (the target's MS/TP MAC
 * address is operator-configured on the xEdge side, matching every other
 * driver's explicit-address model -- Modbus needs a slave ID, this needs
 * a MAC, neither auto-discovers), no request queueing (one in flight at
 * a time, mirroring how the Python driver polls its tags sequentially),
 * and value type coercion (e.g. "is this Enumerated actually a boolean
 * BinaryPV") is deliberately left to the Python side, which already has
 * that logic for the BACnet/IP driver (xedge/drivers/bacnet/client.py) --
 * keeping the C surface a plain protocol pipe, not a second place that
 * semantic coercion could drift out of sync.
 */
#include <errno.h>
#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>

/* BACnet Stack defines - first */
#include "bacnet/bacdef.h"
/* BACnet Stack API */
#include "bacnet/apdu.h"
#include "bacnet/bacapp.h"
#include "bacnet/bacerror.h"
#include "bacnet/bactext.h"
#include "bacnet/npdu.h"
#include "bacnet/rp.h"
#include "bacnet/version.h"
#include "bacnet/basic/binding/address.h"
#include "bacnet/basic/object/device.h"
#include "bacnet/basic/service/s_rp.h"
#include "bacnet/basic/services.h"
#include "bacnet/basic/sys/cJSON.h"
#include "bacnet/basic/tsm/tsm.h"
#include "bacnet/datalink/datalink.h"
#include "bacnet/datalink/dlenv.h"

#define IPC_RX_BUF_SIZE 8192
#define IPC_LINE_MAX 4096

/* --- in-flight request/response state (one at a time) --- */
static uint8_t Request_Invoke_ID = 0;
static BACNET_ADDRESS Request_Target_Address;
static bool Response_Ready = false;
static bool Response_Is_Error = false;
static char Response_Error_Text[128] = { 0 };
static BACNET_APPLICATION_DATA_VALUE Response_Value;

/* buffer used for receive */
static uint8_t Rx_Buf[MAX_MPDU] = { 0 };

static void set_response_error(const char *text)
{
    Response_Is_Error = true;
    snprintf(Response_Error_Text, sizeof(Response_Error_Text), "%s", text);
    Response_Ready = true;
}

static void My_Error_Handler(
    BACNET_ADDRESS *src,
    uint8_t invoke_id,
    BACNET_ERROR_CLASS error_class,
    BACNET_ERROR_CODE error_code)
{
    char text[128];

    if (address_match(&Request_Target_Address, src) &&
        (invoke_id == Request_Invoke_ID)) {
        snprintf(
            text, sizeof(text), "BACnet-Error: %s: %s",
            bactext_error_class_name((int)error_class),
            bactext_error_code_name((int)error_code));
        set_response_error(text);
    }
}

static void My_Abort_Handler(
    BACNET_ADDRESS *src, uint8_t invoke_id, uint8_t abort_reason, bool server)
{
    char text[128];

    (void)server;
    if (address_match(&Request_Target_Address, src) &&
        (invoke_id == Request_Invoke_ID)) {
        snprintf(
            text, sizeof(text), "BACnet-Abort: %s",
            bactext_abort_reason_name((int)abort_reason));
        set_response_error(text);
    }
}

static void My_Reject_Handler(
    BACNET_ADDRESS *src, uint8_t invoke_id, uint8_t reject_reason)
{
    char text[128];

    if (address_match(&Request_Target_Address, src) &&
        (invoke_id == Request_Invoke_ID)) {
        snprintf(
            text, sizeof(text), "BACnet-Reject: %s",
            bactext_reject_reason_name((int)reject_reason));
        set_response_error(text);
    }
}

static void My_Read_Property_Ack_Handler(
    uint8_t *service_request,
    uint16_t service_len,
    BACNET_ADDRESS *src,
    BACNET_CONFIRMED_SERVICE_ACK_DATA *service_data)
{
    int len;
    BACNET_READ_PROPERTY_DATA data;

    if (!address_match(&Request_Target_Address, src) ||
        (service_data->invoke_id != Request_Invoke_ID)) {
        return;
    }
    len = rp_ack_decode_service_request(service_request, service_len, &data);
    if (len < 0) {
        set_response_error("decode failed");
        return;
    }
    len = bacapp_decode_application_data(
        data.application_data, (uint32_t)data.application_data_len,
        &Response_Value);
    if (len < 0) {
        set_response_error("value decode failed");
        return;
    }
    Response_Is_Error = false;
    Response_Ready = true;
}

static void Init_Service_Handlers(void)
{
    Device_Init(NULL);
    apdu_set_unrecognized_service_handler_handler(handler_unrecognized_service);
    apdu_set_confirmed_ack_handler(
        SERVICE_CONFIRMED_READ_PROPERTY, My_Read_Property_Ack_Handler);
    apdu_set_error_handler(SERVICE_CONFIRMED_READ_PROPERTY, My_Error_Handler);
    apdu_set_abort_handler(My_Abort_Handler);
    apdu_set_reject_handler(My_Reject_Handler);
}

/* --- JSON output ---
 * bacnet-stack's vendored cJSON.h (src/bacnet/basic/sys/cJSON.h) is
 * explicitly "Trimmed single-header cJSON for embedded use" -- parsing
 * only (cJSON_Parse/GetObjectItem/Is-prefixed type checks) -- no value
 * construction or printing functions at all. It's used elsewhere in the
 * library to read JSON config files, not to emit JSON. Used for parsing
 * incoming requests below; outgoing
 * responses are hand-formatted here instead -- the response shape is a
 * small, fixed set of flat templates, so this needs no general-purpose
 * serializer, just correct string escaping. */
static void json_escape_into(char *dst, size_t dst_size, const char *src)
{
    size_t di = 0;

    for (; *src != '\0'; src++) {
        char esc = '\0';

        if (di + 2 >= dst_size) {
            break;
        }
        switch (*src) {
            case '"':
                esc = '"';
                break;
            case '\\':
                esc = '\\';
                break;
            case '\n':
                esc = 'n';
                break;
            case '\r':
                esc = 'r';
                break;
            case '\t':
                esc = 't';
                break;
            default:
                break;
        }
        if (esc != '\0') {
            dst[di++] = '\\';
            dst[di++] = esc;
        } else if ((unsigned char)*src >= 0x20) {
            dst[di++] = *src;
        }
        /* other control characters (< 0x20) are dropped rather than
           risking invalid JSON output */
    }
    dst[di] = '\0';
}

/* --- Unix domain socket IPC --- */
static int ipc_listen(const char *socket_path)
{
    int fd;
    struct sockaddr_un addr;

    unlink(socket_path);
    fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK, 0);
    if (fd < 0) {
        perror("socket");
        exit(1);
    }
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", socket_path);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind");
        exit(1);
    }
    if (listen(fd, 1) < 0) {
        perror("listen");
        exit(1);
    }
    return fd;
}

static void ipc_send_line(int fd, const char *text)
{
    size_t len = strlen(text);
    /* best-effort -- if the client went away mid-write there is no
       recovery action beyond dropping the connection on the next loop */
    if (write(fd, text, len) < 0) {
        return;
    }
    if (write(fd, "\n", 1) < 0) {
        return;
    }
}

static void ipc_send_error(int fd, const char *reason)
{
    char escaped[192];
    char line[256];

    json_escape_into(escaped, sizeof(escaped), reason);
    snprintf(line, sizeof(line), "{\"ok\":false,\"error\":\"%s\"}", escaped);
    ipc_send_line(fd, line);
}

static void ipc_send_value(int fd, const BACNET_APPLICATION_DATA_VALUE *value)
{
    char line[512];
    char escaped[400];
    double real_value;

    switch (value->tag) {
        case BACNET_APPLICATION_TAG_BOOLEAN:
            snprintf(
                line, sizeof(line), "{\"ok\":true,\"value\":%s}",
                value->type.Boolean ? "true" : "false");
            break;
        case BACNET_APPLICATION_TAG_UNSIGNED_INT:
            snprintf(
                line, sizeof(line), "{\"ok\":true,\"value\":%llu}",
                (unsigned long long)value->type.Unsigned_Int);
            break;
        case BACNET_APPLICATION_TAG_SIGNED_INT:
            snprintf(
                line, sizeof(line), "{\"ok\":true,\"value\":%ld}",
                (long)value->type.Signed_Int);
            break;
        case BACNET_APPLICATION_TAG_REAL:
        case BACNET_APPLICATION_TAG_DOUBLE:
            /* a BACnet REAL/DOUBLE can legitimately encode NaN/Infinity
               (used as device-specific "not set"/error sentinels) --
               neither is valid JSON, so those become null rather than
               emitting unparsable output */
            real_value = (value->tag == BACNET_APPLICATION_TAG_REAL)
                ? (double)value->type.Real
                : value->type.Double;
            if (isnan(real_value) || isinf(real_value)) {
                snprintf(line, sizeof(line), "{\"ok\":true,\"value\":null}");
            } else {
                /* %.17g round-trips a float/double exactly (a plain %g's
                   default 6-digit precision would silently lose precision
                   on every read) -- but %g also drops the decimal point
                   entirely for a whole-number value (e.g. "0" rather than
                   "0.0"), which is syntactically a JSON integer, not a
                   float: json.loads("0") on the Python side returns int,
                   silently losing this value's actual BACnet REAL/DOUBLE
                   type. Force a trailing ".0" whenever %g didn't already
                   produce a '.' or exponent, so the JSON text itself
                   marks this as a float. */
                char numbuf[64];
                snprintf(numbuf, sizeof(numbuf), "%.17g", real_value);
                /* NaN/Infinity are already handled above and never reach
                   here, so only '.'/exponent need checking */
                if (strpbrk(numbuf, ".eE") == NULL) {
                    size_t len = strlen(numbuf);
                    if (len + 3 <= sizeof(numbuf)) {
                        snprintf(numbuf + len, sizeof(numbuf) - len, ".0");
                    }
                }
                snprintf(line, sizeof(line), "{\"ok\":true,\"value\":%s}", numbuf);
            }
            break;
        case BACNET_APPLICATION_TAG_ENUMERATED:
            snprintf(
                line, sizeof(line), "{\"ok\":true,\"value\":%lu}",
                (unsigned long)value->type.Enumerated);
            break;
        case BACNET_APPLICATION_TAG_CHARACTER_STRING:
            json_escape_into(
                escaped, sizeof(escaped),
                characterstring_value(
                    (BACNET_CHARACTER_STRING *)&value->type.Character_String));
            snprintf(
                line, sizeof(line), "{\"ok\":true,\"value\":\"%s\"}", escaped);
            break;
        default:
            ipc_send_error(fd, "unsupported value type");
            return;
    }
    ipc_send_line(fd, line);
}

/**
 * Parses one request line and starts the BACnet ReadProperty transaction.
 * Returns true if a request was actually started (caller should now wait
 * for Response_Ready), false if the line was malformed (caller has
 * already been sent an error and should look for the next line).
 */
static bool start_request(int client_fd, const char *line)
{
    cJSON *root, *item;
    uint32_t device_instance, object_instance, object_type_val, property_val;
    unsigned mac;
    BACNET_ADDRESS dest;
    unsigned max_apdu = MAX_APDU;

    root = cJSON_Parse(line);
    if (root == NULL) {
        ipc_send_error(client_fd, "invalid JSON");
        return false;
    }
    item = cJSON_GetObjectItem(root, "device_instance");
    if (!cJSON_IsNumber(item)) {
        cJSON_Delete(root);
        ipc_send_error(client_fd, "device_instance must be a number");
        return false;
    }
    device_instance = (uint32_t)item->valuedouble;
    item = cJSON_GetObjectItem(root, "mac");
    if (!cJSON_IsNumber(item) || item->valuedouble < 0 ||
        item->valuedouble > 255) {
        cJSON_Delete(root);
        ipc_send_error(client_fd, "mac must be a number 0-255");
        return false;
    }
    mac = (unsigned)item->valuedouble;
    item = cJSON_GetObjectItem(root, "object_type");
    if (!cJSON_IsString(item) ||
        !bactext_object_type_strtol(item->valuestring, &object_type_val)) {
        cJSON_Delete(root);
        ipc_send_error(client_fd, "unknown object_type");
        return false;
    }
    item = cJSON_GetObjectItem(root, "object_instance");
    if (!cJSON_IsNumber(item)) {
        cJSON_Delete(root);
        ipc_send_error(client_fd, "object_instance must be a number");
        return false;
    }
    object_instance = (uint32_t)item->valuedouble;
    item = cJSON_GetObjectItem(root, "property_id");
    if (!cJSON_IsString(item) ||
        !bactext_property_strtol(item->valuestring, &property_val)) {
        cJSON_Delete(root);
        ipc_send_error(client_fd, "unknown property_id");
        return false;
    }
    cJSON_Delete(root);

    memset(&dest, 0, sizeof(dest));
    dest.mac_len = 1;
    dest.mac[0] = (uint8_t)mac;
    dest.net = 0;
    address_add(device_instance, max_apdu, &dest);

    Request_Target_Address = dest;
    Response_Ready = false;
    Response_Is_Error = false;
    Request_Invoke_ID = Send_Read_Property_Request(
        device_instance, (BACNET_OBJECT_TYPE)object_type_val, object_instance,
        (BACNET_PROPERTY_ID)property_val, BACNET_ARRAY_ALL);
    return true;
}

static void print_usage(void)
{
    fprintf(
        stderr,
        "Usage: xedge-bacnet-mstp-daemon --iface DEV --socket PATH "
        "--device-instance N [--mac M] [--baud B] "
        "[--max-info-frames F] [--max-master M]\n");
}

int main(int argc, char *argv[])
{
    const char *iface = NULL;
    const char *socket_path = NULL;
    long device_instance = -1;
    long mac_address = 127;
    long baud_rate = 38400;
    long max_info_frames = 1;
    long max_master = 127;
    int argi;
    int listen_fd, client_fd = -1;
    char rx_buf[IPC_RX_BUF_SIZE];
    size_t rx_len = 0;
    bool request_pending = false;
    int pending_client_fd = -1;
    time_t last_seconds, current_seconds, request_started_at = 0;
    BACNET_ADDRESS src = { 0 };
    uint16_t pdu_len;

    for (argi = 1; argi < argc; argi++) {
        if (strcmp(argv[argi], "--iface") == 0 && argi + 1 < argc) {
            iface = argv[++argi];
        } else if (strcmp(argv[argi], "--socket") == 0 && argi + 1 < argc) {
            socket_path = argv[++argi];
        } else if (strcmp(argv[argi], "--device-instance") == 0 && argi + 1 < argc) {
            device_instance = strtol(argv[++argi], NULL, 0);
        } else if (strcmp(argv[argi], "--mac") == 0 && argi + 1 < argc) {
            mac_address = strtol(argv[++argi], NULL, 0);
        } else if (strcmp(argv[argi], "--baud") == 0 && argi + 1 < argc) {
            baud_rate = strtol(argv[++argi], NULL, 0);
        } else if (strcmp(argv[argi], "--max-info-frames") == 0 && argi + 1 < argc) {
            max_info_frames = strtol(argv[++argi], NULL, 0);
        } else if (strcmp(argv[argi], "--max-master") == 0 && argi + 1 < argc) {
            max_master = strtol(argv[++argi], NULL, 0);
        } else {
            print_usage();
            return 1;
        }
    }
    if (!iface || !socket_path || device_instance < 0) {
        print_usage();
        return 1;
    }

    /* dlenv_init()'s MS/TP path reads these -- see
       third_party/bacnet-stack/src/bacnet/datalink/dlenv.c
       (dlenv_network_port_mstp_init / datalink_init). Reusing this proven
       env-var-driven init path rather than calling the dlmstp setter
       functions and dlmstp_init() directly by hand. */
    setenv("BACNET_IFACE", iface, 1);
    {
        char numbuf[32];
        snprintf(numbuf, sizeof(numbuf), "%ld", mac_address);
        setenv("BACNET_MSTP_MAC", numbuf, 1);
        snprintf(numbuf, sizeof(numbuf), "%ld", baud_rate);
        setenv("BACNET_MSTP_BAUD", numbuf, 1);
        snprintf(numbuf, sizeof(numbuf), "%ld", max_info_frames);
        setenv("BACNET_MAX_INFO_FRAMES", numbuf, 1);
        snprintf(numbuf, sizeof(numbuf), "%ld", max_master);
        setenv("BACNET_MAX_MASTER", numbuf, 1);
    }

    address_init();
    Device_Set_Object_Instance_Number((uint32_t)device_instance);
    Init_Service_Handlers();
    dlenv_init();
    atexit(datalink_cleanup);
    signal(SIGPIPE, SIG_IGN);

    listen_fd = ipc_listen(socket_path);
    fprintf(
        stderr,
        "xedge-bacnet-mstp-daemon: iface=%s mac=%ld baud=%ld socket=%s\n",
        iface, mac_address, baud_rate, socket_path);

    last_seconds = time(NULL);
    for (;;) {
        current_seconds = time(NULL);
        if (current_seconds != last_seconds) {
            tsm_timer_milliseconds(
                (uint16_t)((current_seconds - last_seconds) * 1000));
            datalink_maintenance_timer(current_seconds - last_seconds);
        }
        last_seconds = current_seconds;

        if (client_fd < 0) {
            client_fd = accept(listen_fd, NULL, NULL);
            if (client_fd >= 0) {
                rx_len = 0;
            }
        }

        if (client_fd >= 0 && !request_pending && rx_len < sizeof(rx_buf) - 1) {
            ssize_t n = read(client_fd, rx_buf + rx_len, sizeof(rx_buf) - 1 - rx_len);
            if (n > 0) {
                rx_len += (size_t)n;
                rx_buf[rx_len] = '\0';
            } else if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) {
                /* client disconnected */
                close(client_fd);
                client_fd = -1;
                rx_len = 0;
            }
            {
                char *newline = memchr(rx_buf, '\n', rx_len);
                if (newline != NULL) {
                    size_t line_len = (size_t)(newline - rx_buf);
                    char line[IPC_LINE_MAX];
                    size_t remaining;

                    if (line_len >= sizeof(line)) {
                        line_len = sizeof(line) - 1;
                    }
                    memcpy(line, rx_buf, line_len);
                    line[line_len] = '\0';
                    remaining = rx_len - (size_t)(newline - rx_buf) - 1;
                    memmove(rx_buf, newline + 1, remaining);
                    rx_len = remaining;

                    if (start_request(client_fd, line)) {
                        request_pending = true;
                        pending_client_fd = client_fd;
                        request_started_at = current_seconds;
                    }
                }
            }
        }

        /* service the datalink continuously, regardless of whether a
           request is pending -- the master must keep participating in
           MS/TP token-passing even when it has nothing to send, or it
           risks being dropped from the ring. */
        pdu_len = datalink_receive(&src, Rx_Buf, MAX_MPDU, 50);
        if (pdu_len) {
            npdu_handler(&src, Rx_Buf, pdu_len);
        }

        if (request_pending) {
            if (Response_Ready) {
                if (Response_Is_Error) {
                    ipc_send_error(pending_client_fd, Response_Error_Text);
                } else {
                    ipc_send_value(pending_client_fd, &Response_Value);
                }
                if (!tsm_invoke_id_free(Request_Invoke_ID)) {
                    tsm_free_invoke_id(Request_Invoke_ID);
                }
                request_pending = false;
            } else if (tsm_invoke_id_failed(Request_Invoke_ID)) {
                tsm_free_invoke_id(Request_Invoke_ID);
                ipc_send_error(pending_client_fd, "timeout");
                request_pending = false;
            } else if ((current_seconds - request_started_at) > 10) {
                /* belt-and-suspenders -- TSM's own apdu_timeout()*retries
                   should trip first, but never hang a client forever */
                tsm_free_invoke_id(Request_Invoke_ID);
                ipc_send_error(pending_client_fd, "timeout");
                request_pending = false;
            }
        }
    }

    return 0;
}
