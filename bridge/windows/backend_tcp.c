/*
 * SmartSoft bridge -- TCP backend: relays each bulk transfer to ConsoleLink on the Mac
 * (`consolelink --smartsoft-bridge`), which replays it on the real console.
 *
 * Frames, both ways, little-endian: [op:1][status:1][reserved:2][length:4] + length bytes.
 * Requests (replies carry op | 0x80):
 *   HELLO 0x01 [version:2][flags:2]      -> [version:2][vid:2][pid:2][bcd:2][product ASCII]
 *   WRITE 0x02 [data]                    -> [written:4]
 *   READ  0x03 [max_len:4][timeout_ms:4] -> [data]
 * Status: 0 ok, 1 timeout, 2 no console, 3 I/O error, 4 bad request. HELLO flag 1 starts a
 * session (the Mac side drops replies left over from an interrupted one).
 *
 * Address: SMARTSOFT_BRIDGE=tcp (127.0.0.1:47561), tcp:PORT or tcp:HOST:PORT. CrossOver
 * programs are ordinary Mac processes, so 127.0.0.1 is the Mac itself.
 */
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdlib.h>
#include <string.h>
#include "bridge.h"

#define PROTOCOL_VERSION 1
#define DEFAULT_PORT "47561"
#define OP_HELLO 0x01
#define OP_WRITE 0x02
#define OP_READ  0x03
#define OP_REPLY 0x80
#define ST_OK      0
#define ST_TIMEOUT 1
#define HELLO_SESSION_START 0x0001
#define IO_SLACK_MS 5000          /* socket timeout beyond the requested USB timeout */
/* While ConsoleLink is unreachable or has no console, each transfer fails only after this long. SmartSoft retries
   a failed write at once; a real driver would block on a dead device, so failing instantly
   would spin SmartSoft's poll loop (thousands of attempts a second). */
#define RETRY_DELAY_MS 200
#define MAX_REPLY  (64 * 1024)

static char host[128] = "127.0.0.1";
static char port[16] = DEFAULT_PORT;

struct bridge_session {
    SOCKET sock;
    int reconnects;
};

static INIT_ONCE wsa_once = INIT_ONCE_STATIC_INIT;

static BOOL CALLBACK wsa_init(PINIT_ONCE once, PVOID param, PVOID *ctx)
{
    (void)once; (void)param; (void)ctx;
    WSADATA wsa;
    return WSAStartup(MAKEWORD(2, 2), &wsa) == 0;
}

/* SMARTSOFT_BRIDGE value after "tcp": "", ":PORT" or ":HOST:PORT". */
void tcp_backend_configure(const char *spec)
{
    if (!spec || spec[0] != ':')
        return;
    spec++;
    const char *colon = strrchr(spec, ':');
    if (colon) {
        size_t n = (size_t)(colon - spec);
        if (n > 0 && n < sizeof(host)) {
            memcpy(host, spec, n);
            host[n] = 0;
        }
        spec = colon + 1;
    }
    if (*spec && strlen(spec) < sizeof(port))
        strcpy(port, spec);
}

const char *tcp_backend_address(void)
{
    static char addr[160];
    strcpy(addr, host);
    strcat(addr, ":");
    strcat(addr, port);
    return addr;
}

/* Logs only when reachability changes, not on every enumeration. */
static void report(int reachable, int err)
{
    static int last = -1;
    if (reachable == last)
        return;
    last = reachable;
    if (reachable)
        bridge_log("tcp: ConsoleLink reachable at %s", tcp_backend_address());
    else
        bridge_log("tcp: ConsoleLink not reachable at %s (error %d): is "
                   "`consolelink --smartsoft-bridge` running?", tcp_backend_address(), err);
}

static void set_timeout(SOCKET s, DWORD ms)
{
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&ms, sizeof(ms));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, (const char *)&ms, sizeof(ms));
}

static SOCKET connect_bridge(void)
{
    if (!InitOnceExecuteOnce(&wsa_once, wsa_init, NULL, NULL)) {
        report(0, -1);
        return INVALID_SOCKET;
    }
    struct addrinfo hints = {0}, *res = NULL;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    int err = getaddrinfo(host, port, &hints, &res);
    if (err) {
        report(0, err);
        return INVALID_SOCKET;
    }
    SOCKET s = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (s != INVALID_SOCKET && connect(s, res->ai_addr, (int)res->ai_addrlen) != 0) {
        err = WSAGetLastError();
        closesocket(s);
        s = INVALID_SOCKET;
    }
    freeaddrinfo(res);
    if (s == INVALID_SOCKET) {
        report(0, err);
        return s;
    }
    BOOL on = TRUE;
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (const char *)&on, sizeof(on));
    set_timeout(s, IO_SLACK_MS);
    return s;
}

static int send_all(SOCKET s, const void *buf, unsigned n)
{
    const char *p = buf;
    while (n) {
        int r = send(s, p, (int)n, 0);
        if (r <= 0)
            return -1;
        p += r;
        n -= (unsigned)r;
    }
    return 0;
}

static int recv_all(SOCKET s, void *buf, unsigned n)
{
    char *p = buf;
    while (n) {
        int r = recv(s, p, (int)n, 0);
        if (r <= 0)
            return -1;
        p += r;
        n -= (unsigned)r;
    }
    return 0;
}

/* One request/reply. Returns the reply status (>= 0) with *reply_len bytes in reply, or -1 if
   the connection failed (the caller drops it). */
static int request(SOCKET s, unsigned char op, const void *payload, unsigned len,
                   void *reply, unsigned reply_cap, unsigned *reply_len)
{
    unsigned char hdr[8] = {op, 0, 0, 0, (unsigned char)len, (unsigned char)(len >> 8),
                            (unsigned char)(len >> 16), (unsigned char)(len >> 24)};
    if (send_all(s, hdr, sizeof(hdr)) || (len && send_all(s, payload, len)))
        return -1;
    if (recv_all(s, hdr, sizeof(hdr)))
        return -1;
    unsigned rlen = hdr[4] | hdr[5] << 8 | hdr[6] << 16 | (unsigned)hdr[7] << 24;
    if (hdr[0] != (op | OP_REPLY) || rlen > reply_cap || rlen > MAX_REPLY) {
        bridge_log("tcp: unexpected reply (op %#x, %u bytes)", hdr[0], rlen);
        return -1;
    }
    if (rlen && recv_all(s, reply, rlen))
        return -1;
    *reply_len = rlen;
    return hdr[1];
}

/* HELLO; fills id when given. Returns the status, or -1 on a connection failure. */
static int hello(SOCKET s, unsigned short flags, bridge_identity *id)
{
    unsigned char req[4] = {PROTOCOL_VERSION, 0, (unsigned char)flags,
                            (unsigned char)(flags >> 8)};
    unsigned char rep[8 + sizeof(id->product)];
    unsigned n = 0;
    int st = request(s, OP_HELLO, req, sizeof(req), rep, sizeof(rep) - 1, &n);
    if (st == ST_OK && id) {
        if (n < 8)
            return -1;
        id->vid = rep[2] | rep[3] << 8;
        id->pid = rep[4] | rep[5] << 8;
        id->bcd = rep[6] | rep[7] << 8;
        memcpy(id->product, rep + 8, n - 8);
        id->product[n - 8] = 0;
    }
    return st;
}

static int tcp_present(void)
{
    SOCKET s = connect_bridge();
    if (s == INVALID_SOCKET)
        return 0;
    int st = hello(s, 0, NULL);
    closesocket(s);
    report(1, 0);
    if (st != ST_OK)
        bridge_log("tcp: ConsoleLink is running but has no console (status %d)", st);
    return st == ST_OK;
}

static SOCKET start_session(bridge_identity *id)
{
    SOCKET s = connect_bridge();
    if (s == INVALID_SOCKET)
        return s;
    report(1, 0);
    bridge_identity tmp;
    int st = hello(s, HELLO_SESSION_START, id ? id : &tmp);
    if (st != ST_OK) {
        bridge_log("tcp: session refused (status %d)", st);
        closesocket(s);
        return INVALID_SOCKET;
    }
    return s;
}

static bridge_session *tcp_open(bridge_identity *id)
{
    SOCKET s = start_session(id);
    if (s == INVALID_SOCKET)
        return NULL;
    bridge_session *sess = calloc(1, sizeof(*sess));
    if (!sess) {
        closesocket(s);
        return NULL;
    }
    sess->sock = s;
    bridge_log("tcp: session started");
    return sess;
}

static void tcp_close(bridge_session *s)
{
    if (s->sock != INVALID_SOCKET)
        closesocket(s->sock);
    bridge_log("tcp: session closed (%d reconnects)", s->reconnects);
    free(s);
}

/* After a broken connection (ConsoleLink restarted), try one new session before failing. */
static int ensure_connected(bridge_session *s)
{
    if (s->sock != INVALID_SOCKET)
        return 1;
    s->sock = start_session(NULL);
    if (s->sock == INVALID_SOCKET) {
        Sleep(RETRY_DELAY_MS);
        return 0;
    }
    s->reconnects++;
    bridge_log("tcp: reconnected");
    return 1;
}

static void drop(bridge_session *s)
{
    if (s->sock != INVALID_SOCKET) {
        closesocket(s->sock);
        s->sock = INVALID_SOCKET;
        bridge_log("tcp: connection to ConsoleLink lost");
    }
}

static int tcp_write(bridge_session *s, const void *buf, unsigned n)
{
    if (!ensure_connected(s))
        return -1;
    unsigned char rep[4];
    unsigned rn = 0;
    set_timeout(s->sock, IO_SLACK_MS);
    int st = request(s->sock, OP_WRITE, buf, n, rep, sizeof(rep), &rn);
    if (st < 0) {
        drop(s);
        return -1;
    }
    if (st == ST_TIMEOUT)
        return 0;
    if (st != ST_OK || rn != 4) {
        Sleep(RETRY_DELAY_MS);   /* e.g. the console was unplugged: ConsoleLink answers at once */
        return -1;
    }
    return (int)(rep[0] | rep[1] << 8 | rep[2] << 16 | (unsigned)rep[3] << 24);
}

static int tcp_read(bridge_session *s, void *buf, unsigned n, unsigned timeout_ms)
{
    if (!ensure_connected(s))
        return -1;
    unsigned char req[8] = {(unsigned char)n, (unsigned char)(n >> 8), (unsigned char)(n >> 16),
                            (unsigned char)(n >> 24), (unsigned char)timeout_ms,
                            (unsigned char)(timeout_ms >> 8), (unsigned char)(timeout_ms >> 16),
                            (unsigned char)(timeout_ms >> 24)};
    unsigned rn = 0;
    set_timeout(s->sock, timeout_ms + IO_SLACK_MS);
    int st = request(s->sock, OP_READ, req, sizeof(req), buf, n, &rn);
    if (st < 0) {
        drop(s);
        return -1;
    }
    if (st == ST_TIMEOUT)
        return 0;
    if (st != ST_OK) {
        Sleep(RETRY_DELAY_MS);
        return -1;
    }
    return (int)rn;
}

const struct bridge_backend tcp_backend = {
    "tcp", tcp_present, tcp_open, tcp_close, tcp_write, tcp_read,
};
