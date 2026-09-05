/* Shared FE/BE wire messages.
 *
 * Layout is the protocol: Crust uses the same sizeof(void*) (8) and the same
 * SysV-style member layout under native and `--target wasm`, so a POD struct
 * written on one side is readable on the other with no glue schema.
 *
 * Keep this header in the C subset Crust accepts (no C++ classes, no bitfields,
 * no flexible array members). Pointer fields are legal but mean addresses in
 * that side's address space -- only integers and fixed arrays travel on the wire.
 */
#ifndef WIREPROTO_MESSAGES_H
#define WIREPROTO_MESSAGES_H

enum WireTag {
    WIRE_PING = 1,
    WIRE_PONG = 2,
    WIRE_ADD  = 3,
    WIRE_SUM  = 4
};

/* Request: tag + two operands. 12 bytes, no padding. */
struct WireReq {
    int tag;
    int a;
    int b;
};

/* Reply: tag + one result. 8 bytes, no padding. */
struct WireRep {
    int tag;
    int result;
};

#endif
