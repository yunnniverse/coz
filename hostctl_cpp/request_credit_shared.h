#ifndef HOSTCTL_REQUEST_CREDIT_SHARED_H
#define HOSTCTL_REQUEST_CREDIT_SHARED_H

#ifdef __BPF__
#include <linux/types.h>
typedef __u8 mcoz_u8;
typedef __u32 mcoz_u32;
typedef __u64 mcoz_u64;
typedef __s64 mcoz_s64;
#else
#include <cstdint>
typedef uint8_t mcoz_u8;
typedef uint32_t mcoz_u32;
typedef uint64_t mcoz_u64;
typedef int64_t mcoz_s64;
#endif

#define MCOZ_MAX_SOURCE_SLOTS 8
#define MCOZ_INVALID_SOURCE_SLOT 0xFFFFFFFFu

struct mcoz_source_credit_slot {
    mcoz_u64 source_key;
    mcoz_u64 delay_ns;
    mcoz_s64 credits;
};

struct mcoz_victim_cfg {
    mcoz_u8 enabled;
    mcoz_u8 reserved0[7];
    mcoz_u32 rr_cursor;
    mcoz_u32 reserved1;
    struct mcoz_source_credit_slot slots[MCOZ_MAX_SOURCE_SLOTS];
};

struct mcoz_consume_event {
    mcoz_u64 ts_ns;
    mcoz_u64 cgroup_id;
    mcoz_u64 delay_ns;
    mcoz_u64 source_key;
    mcoz_u32 tid;
    mcoz_u32 tgid;
    mcoz_u32 syscall_id;
    mcoz_u32 slot_index;
};

#endif  // HOSTCTL_REQUEST_CREDIT_SHARED_H
