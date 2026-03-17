#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "../request_credit_shared.h"

#ifndef BPF_MAP_TYPE_RINGBUF
#define BPF_MAP_TYPE_RINGBUF 27
#endif

#define __NR_read 0
#define __NR_pread64 17
#define __NR_readv 19
#define __NR_recvfrom 45
#define __NR_recvmsg 47
#define __NR_recvmmsg 299
#define __NR_io_uring_setup 425
#define __NR_io_uring_enter 426

#define MCOZ_CONSUME_RECVFROM      (1U << 0)
#define MCOZ_CONSUME_RECVMSG       (1U << 1)
#define MCOZ_CONSUME_RECVMMSG      (1U << 2)
#define MCOZ_CONSUME_READ          (1U << 3)
#define MCOZ_CONSUME_READV         (1U << 4)
#define MCOZ_CONSUME_PREAD64       (1U << 5)
#define MCOZ_CONSUME_IO_URING      (1U << 6)
#define MCOZ_CONSUME_DEFAULT_FLAGS (MCOZ_CONSUME_RECVFROM | MCOZ_CONSUME_RECVMSG | MCOZ_CONSUME_RECVMMSG)

typedef struct mcoz_victim_cfg victim_cfg;
typedef struct mcoz_source_credit_slot source_credit_slot;
typedef struct mcoz_consume_event consume_event;

struct global_stats {
    __u64 bpf_events;
    __u64 ringbuf_drops;
    __u64 consume_triggers;
};

struct runtime_flags {
    __u8 raw_consume_enabled;
    __u8 read_exit_hook_enabled;
    __u8 reserved[6];
};

struct syscall_hist_key {
    __u64 cgroup_id;
    __u32 syscall_id;
    __u32 reserved;
};

struct consume_policy {
    __u32 flags;
    __u32 reserved;
};

struct trace_event_raw_sys_enter {
    __u16 common_type;
    __u8 common_flags;
    __u8 common_preempt_count;
    __s32 common_pid;
    __s64 id;
    __u64 args[6];
};

struct trace_event_raw_sys_exit {
    __u16 common_type;
    __u8 common_flags;
    __u8 common_preempt_count;
    __s32 common_pid;
    __s64 id;
    __s64 ret;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, __u64);
    __type(value, victim_cfg);
} victim_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 1 << 24);
} events SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct global_stats);
} global_stats_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct runtime_flags);
} runtime_flags_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 8192);
    __type(key, __u64);
    __type(value, __u8);
} probe_targets SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 65536);
    __type(key, struct syscall_hist_key);
    __type(value, __u64);
} syscall_hist_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 8192);
    __type(key, __u64);
    __type(value, struct consume_policy);
} consume_policy_map SEC(".maps");

static __always_inline struct global_stats* get_stats(void) {
    __u32 key = 0;
    return bpf_map_lookup_elem(&global_stats_map, &key);
}

static __always_inline void maybe_record_syscall(__u64 cgroup_id, __u32 syscall_id) {
    __u8* enabled = bpf_map_lookup_elem(&probe_targets, &cgroup_id);
    if (!enabled || *enabled == 0) {
        return;
    }
    struct syscall_hist_key key = {};
    key.cgroup_id = cgroup_id;
    key.syscall_id = syscall_id;
    __u64 init_val = 1;
    __u64* cur = bpf_map_lookup_elem(&syscall_hist_map, &key);
    if (cur) {
        __sync_fetch_and_add(cur, 1);
    } else {
        bpf_map_update_elem(&syscall_hist_map, &key, &init_val, BPF_ANY);
    }
}

static __always_inline int should_consume_raw(__u64 cgroup_id, __u32 syscall_id) {
    __u32 flags = MCOZ_CONSUME_DEFAULT_FLAGS;
    struct consume_policy* policy = bpf_map_lookup_elem(&consume_policy_map, &cgroup_id);
    if (policy) {
        flags = policy->flags;
    }

    if (syscall_id == __NR_recvfrom && (flags & MCOZ_CONSUME_RECVFROM)) return 1;
    if (syscall_id == __NR_recvmsg && (flags & MCOZ_CONSUME_RECVMSG)) return 1;
    if (syscall_id == __NR_recvmmsg && (flags & MCOZ_CONSUME_RECVMMSG)) return 1;
    if (syscall_id == __NR_read && (flags & MCOZ_CONSUME_READ)) return 1;
    if (syscall_id == __NR_readv && (flags & MCOZ_CONSUME_READV)) return 1;
    if (syscall_id == __NR_pread64 && (flags & MCOZ_CONSUME_PREAD64)) return 1;
    if ((syscall_id == __NR_io_uring_setup || syscall_id == __NR_io_uring_enter) &&
        (flags & MCOZ_CONSUME_IO_URING)) {
        return 1;
    }
    return 0;
}

static __always_inline int raw_consume_enabled(void) {
    __u32 key = 0;
    struct runtime_flags* flags = bpf_map_lookup_elem(&runtime_flags_map, &key);
    return flags && flags->raw_consume_enabled != 0;
}

static __always_inline int read_exit_hook_enabled(void) {
    __u32 key = 0;
    struct runtime_flags* flags = bpf_map_lookup_elem(&runtime_flags_map, &key);
    return flags && flags->read_exit_hook_enabled != 0;
}

static __always_inline int consume_once(__u32 syscall_id) {
    __u64 cgroup_id = bpf_get_current_cgroup_id();
    victim_cfg* cfg = bpf_map_lookup_elem(&victim_map, &cgroup_id);
    if (!cfg) return 0;
    if (!cfg->enabled) {
        return 0;
    }
    __u32 start = cfg->rr_cursor;
    __u32 chosen = MCOZ_INVALID_SOURCE_SLOT;
    __u64 delay_ns = 0;
    __u64 source_key = 0;

#pragma unroll
    for (int step = 0; step < MCOZ_MAX_SOURCE_SLOTS; ++step) {
        __u32 idx = (start + (__u32)step) % MCOZ_MAX_SOURCE_SLOTS;
        source_credit_slot* slot = &cfg->slots[idx];
        if (slot->source_key == 0 || slot->delay_ns == 0 || slot->credits <= 0) {
            continue;
        }
        chosen = idx;
        delay_ns = slot->delay_ns;
        source_key = slot->source_key;
        cfg->rr_cursor = (idx + 1) % MCOZ_MAX_SOURCE_SLOTS;
        break;
    }
    if (chosen == MCOZ_INVALID_SOURCE_SLOT || delay_ns == 0) {
        return 0;
    }

    struct global_stats* stats = get_stats();
    if (stats) {
        __sync_fetch_and_add(&stats->consume_triggers, 1);
    }

    consume_event* ev = bpf_ringbuf_reserve(&events, sizeof(*ev), 0);
    if (!ev) {
        if (stats) {
            __sync_fetch_and_add(&stats->ringbuf_drops, 1);
        }
        return 0;
    }

    __u64 pid_tgid = bpf_get_current_pid_tgid();
    ev->ts_ns = bpf_ktime_get_ns();
    ev->cgroup_id = cgroup_id;
    ev->delay_ns = delay_ns;
    ev->source_key = source_key;
    ev->tid = (__u32)pid_tgid;
    ev->tgid = (__u32)(pid_tgid >> 32);
    ev->syscall_id = syscall_id;
    ev->slot_index = chosen;
    bpf_ringbuf_submit(ev, 0);

    if (stats) {
        __sync_fetch_and_add(&stats->bpf_events, 1);
    }
    return 0;
}

static __always_inline int consume_if_policy_allows(__u32 syscall_id) {
    __u64 cgroup_id = bpf_get_current_cgroup_id();
    if (!should_consume_raw(cgroup_id, syscall_id)) {
        return 0;
    }
    return consume_once(syscall_id);
}

SEC("tracepoint/syscalls/sys_enter_recvfrom")
int mcoz_on_recvfrom(struct trace_event_raw_sys_enter* ctx) {
    /*
     * When raw consume is enabled, all policy-driven consume is handled in
     * raw_tracepoint/sys_enter to avoid duplicate credit consumption.
     */
    if (raw_consume_enabled()) {
        return 0;
    }
    return consume_if_policy_allows((__u32)ctx->id);
}

SEC("tracepoint/syscalls/sys_enter_recvmsg")
int mcoz_on_recvmsg(struct trace_event_raw_sys_enter* ctx) {
    if (raw_consume_enabled()) {
        return 0;
    }
    return consume_if_policy_allows((__u32)ctx->id);
}

SEC("tracepoint/syscalls/sys_exit_read")
int mcoz_on_read(struct trace_event_raw_sys_exit* ctx) {
    if (ctx->ret <= 0) {
        return 0;
    }
    /*
     * When raw consume is enabled, only keep read consume here if explicitly
     * configured. This avoids read double-consume on enter+exit.
     */
    if (raw_consume_enabled() && !read_exit_hook_enabled()) {
        return 0;
    }
    return consume_if_policy_allows(__NR_read);
}

SEC("raw_tracepoint/sys_enter")
int mcoz_on_raw_sys_enter(struct bpf_raw_tracepoint_args* ctx) {
    __u64 cgroup_id = bpf_get_current_cgroup_id();
    __u64 id = ctx->args[1];
    __u32 syscall_id = (__u32)id;
    maybe_record_syscall(cgroup_id, syscall_id);

    if (!raw_consume_enabled()) {
        return 0;
    }

    /*
     * read는 sys_exit_read(ret>0)에서만 consume하도록 분리할 수 있다.
     * (read_exit_hook_enabled=1)
     */
    if (syscall_id == __NR_read && read_exit_hook_enabled()) {
        return 0;
    }

    if (should_consume_raw(cgroup_id, syscall_id)) {
        return consume_once(syscall_id);
    }
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
