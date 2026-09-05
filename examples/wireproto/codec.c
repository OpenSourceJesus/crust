/* Shared codec for examples/wireproto -- compiled once native, once wasm.
 *
 * Encode/decode copy fields explicitly (little-endian host integers) so the
 * wire bytes do not depend on accidental padding. Layout queries let a test
 * prove native and wasm agree on sizeof/offsets before any bytes move.
 */
#include "messages.h"

int wire_req_size(void) { return (int)sizeof(struct WireReq); }
int wire_rep_size(void) { return (int)sizeof(struct WireRep); }

int wire_req_tag_off(void) {
    return (int)((char *)&((struct WireReq *)0)->tag - (char *)0);
}
int wire_req_a_off(void) {
    return (int)((char *)&((struct WireReq *)0)->a - (char *)0);
}
int wire_req_b_off(void) {
    return (int)((char *)&((struct WireReq *)0)->b - (char *)0);
}
int wire_rep_tag_off(void) {
    return (int)((char *)&((struct WireRep *)0)->tag - (char *)0);
}
int wire_rep_result_off(void) {
    return (int)((char *)&((struct WireRep *)0)->result - (char *)0);
}

void wire_req_encode(unsigned char *buf, int tag, int a, int b) {
    struct WireReq *m = (struct WireReq *)buf;
    m->tag = tag;
    m->a = a;
    m->b = b;
}

int wire_req_decode_tag(unsigned char *buf) {
    return ((struct WireReq *)buf)->tag;
}
int wire_req_decode_a(unsigned char *buf) {
    return ((struct WireReq *)buf)->a;
}
int wire_req_decode_b(unsigned char *buf) {
    return ((struct WireReq *)buf)->b;
}

void wire_rep_encode(unsigned char *buf, int tag, int result) {
    struct WireRep *m = (struct WireRep *)buf;
    m->tag = tag;
    m->result = result;
}

int wire_rep_decode_tag(unsigned char *buf) {
    return ((struct WireRep *)buf)->tag;
}
int wire_rep_decode_result(unsigned char *buf) {
    return ((struct WireRep *)buf)->result;
}

/* Backend handler: ADD -> SUM. Same logic runs in the wasm frontend when the
 * host drives the codec exports directly. */
void wire_handle(unsigned char *req, unsigned char *rep) {
    int tag = wire_req_decode_tag(req);
    if (tag == WIRE_PING) {
        wire_rep_encode(rep, WIRE_PONG, 0);
        return;
    }
    if (tag == WIRE_ADD) {
        int a = wire_req_decode_a(req);
        int b = wire_req_decode_b(req);
        wire_rep_encode(rep, WIRE_SUM, a + b);
        return;
    }
    wire_rep_encode(rep, 0, -1);
}

/* Native self-check / wasm _start: one ADD roundtrip in-process. */
int main(void) {
    unsigned char req[32];
    unsigned char rep[32];
    if (wire_req_size() != 12) return 1;
    if (wire_rep_size() != 8) return 2;
    if (wire_req_tag_off() != 0) return 3;
    if (wire_req_a_off() != 4) return 4;
    if (wire_req_b_off() != 8) return 5;
    wire_req_encode(req, WIRE_ADD, 20, 22);
    wire_handle(req, rep);
    if (wire_rep_decode_tag(rep) != WIRE_SUM) return 6;
    if (wire_rep_decode_result(rep) != 42) return 7;
    return 0;
}
