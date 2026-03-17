#include "request_credit.h"
#include "request_credit_shared.h"

#include "perf.h"

#include <linux/bpf.h>
#ifndef HOSTCTL_HAS_BPF_STATS_TYPE
enum bpf_stats_type {
    BPF_STATS_RUN_TIME = 0,
};
#endif
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/resource.h>
#include <unistd.h>

#include <atomic>
#include <algorithm>
#include <chrono>
#include <cerrno>
#include <cstdint>
#include <cstdarg>
#include <cstdlib>
#include <cstring>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#ifndef MCOZ_REQUEST_BPF_OBJ_PATH
#define MCOZ_REQUEST_BPF_OBJ_PATH "/opt/mcoz/bin/hostctl_cpp/request_credit.bpf.o"
#endif

namespace {
using VictimCfg = mcoz_victim_cfg;
using VictimSourceSlot = mcoz_source_credit_slot;
using ConsumeEvent = mcoz_consume_event;

struct GlobalStatsMapValue {
    uint64_t bpf_events;
    uint64_t ringbuf_drops;
    uint64_t consume_triggers;
};

struct RuntimeFlagsMapValue {
    uint8_t raw_consume_enabled;
    uint8_t read_exit_hook_enabled;
    uint8_t reserved[6];
};

struct ConsumePolicyMapValue {
    uint32_t flags;
    uint32_t reserved;
};

struct SyscallHistKey {
    uint64_t cgroup_id;
    uint32_t syscall_id;
    uint32_t reserved;
};

enum : uint32_t {
    kConsumeRecvfrom = 1u << 0,
    kConsumeRecvmsg = 1u << 1,
    kConsumeRecvmmsg = 1u << 2,
    kConsumeRead = 1u << 3,
    kConsumeReadv = 1u << 4,
    kConsumePread64 = 1u << 5,
    kConsumeIoUring = 1u << 6,
};

static constexpr uint32_t kConsumeDefaultFlags =
    kConsumeRecvfrom | kConsumeRecvmsg | kConsumeRecvmmsg;

struct VictimSourceRuntime {
    uint64_t source_key = 0;
    std::string source;
    uint64_t arm_calls = 0;
    uint64_t armed_total = 0;
    uint64_t consume_trigger = 0;
    uint64_t bpf_events = 0;
    uint64_t injected_ok = 0;
    uint64_t inject_fail = 0;
    uint64_t last_tid = 0;
    uint64_t last_ts_ns = 0;
    uint64_t last_delay_ns = 0;
    std::string last_error;
};

struct VictimRuntime {
    std::string ns;
    std::string pod;
    std::string container;
    std::string cgroup_path;
    uint64_t arm_calls = 0;
    uint64_t armed_total = 0;
    uint64_t consume_trigger = 0;
    uint64_t bpf_events = 0;
    uint64_t injected_ok = 0;
    uint64_t inject_fail = 0;
    uint64_t refund_ok = 0;
    uint64_t refund_fail = 0;
    uint64_t last_tid = 0;
    uint64_t last_ts_ns = 0;
    uint64_t last_delay_ns = 0;
    uint64_t last_source_key = 0;
    std::string last_source;
    std::string last_error;
    std::map<uint64_t, VictimSourceRuntime> sources;
};

static int libbpf_log_fn(enum libbpf_print_level level, const char* fmt, va_list args) {
    if (level == LIBBPF_DEBUG) return 0;
    return vfprintf(stderr, fmt, args);
}

static std::string trim(const std::string& s) {
    if (s.empty()) return s;
    size_t b = s.find_first_not_of(" \n\r\t");
    if (b == std::string::npos) return "";
    size_t e = s.find_last_not_of(" \n\r\t");
    return s.substr(b, e - b + 1);
}

static std::string strip_container_prefix(const std::string& cid) {
    auto pos = cid.find("://");
    if (pos == std::string::npos) return cid;
    return cid.substr(pos + 3);
}

static uint64_t now_unix_ns() {
    auto now = std::chrono::time_point_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now());
    return static_cast<uint64_t>(now.time_since_epoch().count());
}

static uint64_t cgroup_id_from_path(const std::string& path) {
    struct stat st {};
    if (::stat(path.c_str(), &st) != 0) return 0;
    return static_cast<uint64_t>(st.st_ino);
}

static std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (char ch : s) {
        switch (ch) {
            case '"':
                out += "\\\"";
                break;
            case '\\':
                out += "\\\\";
                break;
            case '\n':
                out += "\\n";
                break;
            case '\r':
                out += "\\r";
                break;
            case '\t':
                out += "\\t";
                break;
            default:
                out.push_back(ch);
                break;
        }
    }
    return out;
}

static uint64_t fnv1a64(const std::string& input) {
    uint64_t hash = 14695981039346656037ULL;
    for (unsigned char ch : input) {
        hash ^= static_cast<uint64_t>(ch);
        hash *= 1099511628211ULL;
    }
    if (hash == 0) hash = 1;
    return hash;
}

static std::string format_source_key(uint64_t source_key) {
    std::ostringstream oss;
    oss << "0x" << std::hex << source_key;
    return oss.str();
}

static const char* syscall_name_from_id(uint32_t id) {
    switch (id) {
        case 0:
            return "read";
        case 17:
            return "pread64";
        case 19:
            return "readv";
        case 43:
            return "accept";
        case 45:
            return "recvfrom";
        case 47:
            return "recvmsg";
        case 288:
            return "accept4";
        case 299:
            return "recvmmsg";
        case 425:
            return "io_uring_setup";
        case 426:
            return "io_uring_enter";
        default:
            return "unknown";
    }
}

static std::string consume_flags_to_json_array(uint32_t flags) {
    std::ostringstream oss;
    oss << "[";
    bool first = true;
    auto push = [&](const char* name, bool cond) {
        if (!cond) return;
        if (!first) oss << ",";
        first = false;
        oss << "\"" << name << "\"";
    };
    push("recvfrom", (flags & kConsumeRecvfrom) != 0);
    push("recvmsg", (flags & kConsumeRecvmsg) != 0);
    push("recvmmsg", (flags & kConsumeRecvmmsg) != 0);
    push("read", (flags & kConsumeRead) != 0);
    push("readv", (flags & kConsumeReadv) != 0);
    push("pread64", (flags & kConsumePread64) != 0);
    push("io_uring", (flags & kConsumeIoUring) != 0);
    if (first) {
        oss << "\"none\"";
    }
    oss << "]";
    return oss.str();
}

static uint32_t normalize_consume_flags(uint32_t flags) {
    return flags == 0 ? kConsumeDefaultFlags : flags;
}

static long ghost_delay_tid(pid_t tid, uint64_t ns, int& err_out) {
    errno = 0;
    long r = syscall(449, tid, ns);
    err_out = errno;
    return r;
}

struct ResolvedTarget {
    uint64_t cgroup_id = 0;
    std::string cgroup_path;
    std::string ns;
    std::string pod;
    std::string container;
};

class RequestCreditEngine {
  public:
    ~RequestCreditEngine() {
        stop();
    }

    bool start(uint64_t default_delay_ns, bool refund_on_fail, bool enable_read_hook, std::string& err) {
        std::lock_guard<std::mutex> lock(mu_);
        if (active_) return true;
        default_delay_ns_ = default_delay_ns;
        // Refund-on-fail is intentionally disabled: failed inject remains failed.
        (void)refund_on_fail;
        refund_on_fail_ = false;
        enable_read_hook_ = enable_read_hook;

        struct rlimit rl {};
        rl.rlim_cur = RLIM_INFINITY;
        rl.rlim_max = RLIM_INFINITY;
        (void)setrlimit(RLIMIT_MEMLOCK, &rl);

        libbpf_set_print(libbpf_log_fn);
        libbpf_set_strict_mode(LIBBPF_STRICT_ALL);

        std::string bpf_obj_path = resolve_bpf_obj_path();
        if (bpf_obj_path.empty()) {
            err = "request-credit BPF object not found (set MCOZ_REQUEST_BPF_OBJ)";
            return false;
        }

        bpf_object_open_opts open_opts {};
        open_opts.sz = sizeof(open_opts);
        obj_ = bpf_object__open_file(bpf_obj_path.c_str(), &open_opts);
        if (!obj_) {
            err = "bpf_object__open_file failed: " + bpf_obj_path;
            return false;
        }

        if (!enable_read_hook_) {
            if (bpf_program* p = bpf_object__find_program_by_name(obj_, "mcoz_on_read")) {
                bpf_program__set_autoload(p, false);
            }
        }

        if (bpf_object__load(obj_) != 0) {
            err = "bpf_object__load failed";
            return false;
        }

        victim_map_fd_ = bpf_object__find_map_fd_by_name(obj_, "victim_map");
        global_stats_fd_ = bpf_object__find_map_fd_by_name(obj_, "global_stats_map");
        runtime_flags_fd_ = bpf_object__find_map_fd_by_name(obj_, "runtime_flags_map");
        probe_targets_fd_ = bpf_object__find_map_fd_by_name(obj_, "probe_targets");
        syscall_hist_fd_ = bpf_object__find_map_fd_by_name(obj_, "syscall_hist_map");
        consume_policy_fd_ = bpf_object__find_map_fd_by_name(obj_, "consume_policy_map");
        int ring_fd = bpf_object__find_map_fd_by_name(obj_, "events");
        if (victim_map_fd_ < 0 || global_stats_fd_ < 0 || ring_fd < 0 ||
            runtime_flags_fd_ < 0 || probe_targets_fd_ < 0 || syscall_hist_fd_ < 0 ||
            consume_policy_fd_ < 0) {
            err = "required BPF maps not found";
            return false;
        }

        bpf_program* recvfrom_prog = bpf_object__find_program_by_name(obj_, "mcoz_on_recvfrom");
        bpf_program* recvmsg_prog = bpf_object__find_program_by_name(obj_, "mcoz_on_recvmsg");
        bpf_program* read_prog = bpf_object__find_program_by_name(obj_, "mcoz_on_read");
        bpf_program* raw_prog = bpf_object__find_program_by_name(obj_, "mcoz_on_raw_sys_enter");
        if (!recvfrom_prog || !recvmsg_prog || !raw_prog) {
            err = "required BPF programs not found";
            return false;
        }

        recvfrom_link_ = bpf_program__attach_tracepoint(recvfrom_prog, "syscalls", "sys_enter_recvfrom");
        recvmsg_link_ = bpf_program__attach_tracepoint(recvmsg_prog, "syscalls", "sys_enter_recvmsg");
        bool recv_tracepoints_ready = recvfrom_link_ && recvmsg_link_;
        if (!recv_tracepoints_ready) {
            if (recvfrom_link_) {
                bpf_link__destroy(recvfrom_link_);
                recvfrom_link_ = nullptr;
            }
            if (recvmsg_link_) {
                bpf_link__destroy(recvmsg_link_);
                recvmsg_link_ = nullptr;
            }
        }

        raw_link_ = bpf_program__attach_raw_tracepoint(raw_prog, "sys_enter");
        if (!raw_link_ && !recv_tracepoints_ready) {
            err = "failed to attach recv* tracepoints and raw_tracepoint fallback";
            return false;
        }
        if (raw_link_ && !recv_tracepoints_ready) {
            std::cerr << "[MCOZ] request-credit attach fallback: raw_tracepoint/sys_enter\n";
        }
        if (raw_link_ && recv_tracepoints_ready) {
            std::cerr << "[MCOZ] request-credit consume path: raw_tracepoint/sys_enter (policy-driven)\n";
        }

        if (enable_read_hook_ && read_prog) {
            read_link_ = bpf_program__attach_tracepoint(read_prog, "syscalls", "sys_exit_read");
            if (!read_link_) {
                std::cerr << "[MCOZ] request-credit read hook unavailable: "
                          << "tracepoint/syscalls/sys_exit_read attach failed; "
                          << "continuing without read-exit hook\n";
            }
        }

        RuntimeFlagsMapValue flags {};
        // Keep raw consume enabled whenever raw tracepoint is attached so
        // cgroup consume_policy applies consistently across syscall families.
        flags.raw_consume_enabled = raw_link_ ? 1 : 0;
        // If enabled, read consume is performed on sys_exit_read(ret>0).
        flags.read_exit_hook_enabled = read_link_ ? 1 : 0;
        uint32_t flags_key = 0;
        if (runtime_flags_fd_ >= 0) {
            bpf_map_update_elem(runtime_flags_fd_, &flags_key, &flags, BPF_ANY);
        }

        ring_ = ring_buffer__new(ring_fd, &RequestCreditEngine::on_ring_event_static, this, nullptr);
        if (!ring_) {
            err = "ring_buffer__new failed";
            return false;
        }

        GlobalStatsMapValue zero {};
        uint32_t key = 0;
        bpf_map_update_elem(global_stats_fd_, &key, &zero, BPF_ANY);

        active_ = true;
        running_.store(true, std::memory_order_release);
        poll_thread_ = std::thread([this]() { this->poll_loop(); });

        std::cerr << "[MCOZ] request-credit mode started"
                  << " default_delay_ns=" << default_delay_ns_
                  << " refund_on_fail=" << (refund_on_fail_ ? 1 : 0)
                  << " enable_read_hook=" << (enable_read_hook_ ? 1 : 0)
                  << " bpf_obj=" << bpf_obj_path << "\n";
        return true;
    }

    void stop() {
        running_.store(false, std::memory_order_release);
        if (poll_thread_.joinable()) poll_thread_.join();
        std::lock_guard<std::mutex> lock(mu_);
        stop_unlocked();
    }

    bool is_active() const {
        return active_;
    }

    static int64_t total_credits(const VictimCfg& cfg) {
        int64_t total = 0;
        for (const auto& slot : cfg.slots) {
            if (slot.credits > 0) total += slot.credits;
        }
        return total;
    }

    static uint64_t max_delay(const VictimCfg& cfg) {
        uint64_t out = 0;
        for (const auto& slot : cfg.slots) {
            if (slot.delay_ns > out) out = slot.delay_ns;
        }
        return out;
    }

    static int find_exact_slot(const VictimCfg& cfg, uint64_t source_key) {
        if (source_key == 0) return -1;
        for (size_t i = 0; i < MCOZ_MAX_SOURCE_SLOTS; ++i) {
            if (cfg.slots[i].source_key == source_key) {
                return static_cast<int>(i);
            }
        }
        return -1;
    }

    static int find_free_slot(const VictimCfg& cfg) {
        int empty = -1;
        int reusable = -1;
        for (size_t i = 0; i < MCOZ_MAX_SOURCE_SLOTS; ++i) {
            const auto& slot = cfg.slots[i];
            if (slot.source_key == 0 && slot.credits == 0 && slot.delay_ns == 0) {
                return static_cast<int>(i);
            }
            if (slot.credits == 0 && reusable < 0) {
                reusable = static_cast<int>(i);
            }
            if (slot.source_key == 0 && empty < 0) {
                empty = static_cast<int>(i);
            }
        }
        if (reusable >= 0) return reusable;
        return empty;
    }

    static int find_slot_for_source(VictimCfg& cfg, uint64_t source_key) {
        int exact = find_exact_slot(cfg, source_key);
        if (exact >= 0) return exact;
        return find_free_slot(cfg);
    }

    bool arm(const RequestArmRequest& req, std::string& response_json) {
        triggered_.fetch_add(1, std::memory_order_relaxed);
        if (req.count < 0) {
            arm_fail_.fetch_add(1, std::memory_order_relaxed);
            response_json = "{\"ok\":false,\"error\":\"count must be >= 0\"}";
            return false;
        }

        auto resolved = resolve_target(req.ns, req.pod, req.container);
        if (!resolved.has_value()) {
            arm_fail_.fetch_add(1, std::memory_order_relaxed);
            response_json = "{\"ok\":false,\"state\":\"TRIGGERED\",\"error\":\"failed to resolve cgroup\"}";
            return false;
        }

        std::lock_guard<std::mutex> lock(mu_);
        if (!active_) {
            arm_fail_.fetch_add(1, std::memory_order_relaxed);
            response_json = "{\"ok\":false,\"error\":\"request-credit mode is not active\"}";
            return false;
        }

        uint64_t cgid = resolved->cgroup_id;
        VictimCfg cfg {};
        bool has_cfg = (bpf_map_lookup_elem(victim_map_fd_, &cgid, &cfg) == 0);
        if (!has_cfg) {
            std::memset(&cfg, 0, sizeof(cfg));
        }
        uint64_t existing_delay = max_delay(cfg);
        uint64_t effective_delay = req.delay_ns > 0 ? req.delay_ns : (existing_delay > 0 ? existing_delay : default_delay_ns_);
        cfg.enabled = 1;
        const std::string source_id = req.source.empty() ? "unspecified" : req.source;
        const uint64_t source_key = fnv1a64(source_id);
        int slot_index = find_slot_for_source(cfg, source_key);
        if (slot_index < 0) {
            arm_fail_.fetch_add(1, std::memory_order_relaxed);
            std::ostringstream oss;
            oss << "{\"ok\":false,\"state\":\"TRIGGERED\",\"error\":\"no free source slots\",\"max_slots\":"
                << MCOZ_MAX_SOURCE_SLOTS
                << ",\"source\":\"" << json_escape(source_id) << "\"}";
            response_json = oss.str();
            return false;
        }
        auto& slot = cfg.slots[slot_index];
        const int64_t prev_slot_credits = slot.credits;
        slot.source_key = source_key;
        slot.delay_ns = effective_delay;
        slot.credits += req.count;
        if (slot.credits < 0) slot.credits = 0;
        cfg.rr_cursor = static_cast<uint32_t>(slot_index);
        const int64_t total_after = total_credits(cfg);
        if (bpf_map_update_elem(victim_map_fd_, &cgid, &cfg, BPF_ANY) != 0) {
            arm_fail_.fetch_add(1, std::memory_order_relaxed);
            std::ostringstream oss;
            oss << "{\"ok\":false,\"state\":\"TRIGGERED\",\"error\":\"bpf_map_update_elem failed\",\"errno\":"
                << errno << "}";
            response_json = oss.str();
            return false;
        }

        auto& vr = victims_[cgid];
        vr.ns = resolved->ns;
        vr.pod = resolved->pod;
        vr.container = resolved->container;
        vr.cgroup_path = resolved->cgroup_path;
        vr.arm_calls += 1;
        vr.armed_total += static_cast<uint64_t>(req.count);
        vr.last_delay_ns = effective_delay;
        vr.last_source_key = source_key;
        vr.last_source = source_id;
        vr.last_error.clear();
        auto& sr = vr.sources[source_key];
        sr.source_key = source_key;
        sr.source = source_id;
        sr.arm_calls += 1;
        sr.armed_total += static_cast<uint64_t>(req.count);
        sr.last_delay_ns = effective_delay;
        sr.last_error.clear();

        if (consume_policy_fd_ >= 0) {
            ConsumePolicyMapValue policy {};
            if (bpf_map_lookup_elem(consume_policy_fd_, &cgid, &policy) != 0) {
                policy.flags = kConsumeDefaultFlags;
                bpf_map_update_elem(consume_policy_fd_, &cgid, &policy, BPF_ANY);
            }
        }

        armed_ok_.fetch_add(1, std::memory_order_relaxed);
        if (req.count > 0) {
            std::cerr << "[MCOZ] state=CREDIT_ADD cgroup_id=" << cgid
                      << " ns=" << vr.ns
                      << " pod=" << vr.pod
                      << " container=" << vr.container
                      << " source=" << source_id
                      << " source_key=" << format_source_key(source_key)
                      << " slot=" << slot_index
                      << " slot_credits=" << prev_slot_credits << "->" << slot.credits
                      << " credits_total=" << total_after
                      << " add=" << req.count
                      << " delay_ns=" << slot.delay_ns << "\n";
        } else {
            std::cerr << "[MCOZ] state=CREDIT_NOOP cgroup_id=" << cgid
                      << " ns=" << vr.ns
                      << " pod=" << vr.pod
                      << " container=" << vr.container
                      << " source=" << source_id
                      << " source_key=" << format_source_key(source_key)
                      << " slot=" << slot_index
                      << " slot_credits=" << prev_slot_credits << "->" << slot.credits
                      << " credits_total=" << total_after
                      << " add=" << req.count
                      << " delay_ns=" << slot.delay_ns << "\n";
        }
        std::cerr << "[MCOZ] state=ARMED cgroup_id=" << cgid
                  << " ns=" << vr.ns
                  << " pod=" << vr.pod
                  << " container=" << vr.container
                  << " source=" << source_id
                  << " source_key=" << format_source_key(source_key)
                  << " slot=" << slot_index
                  << " credits=" << total_after
                  << " slot_credits=" << slot.credits
                  << " delay_ns=" << slot.delay_ns
                  << " count=" << req.count << "\n";

        std::ostringstream oss;
        oss << "{\"ok\":true,\"state\":\"ARMED\""
            << ",\"namespace\":\"" << json_escape(vr.ns) << "\""
            << ",\"pod\":\"" << json_escape(vr.pod) << "\""
            << ",\"container\":\"" << json_escape(vr.container) << "\""
            << ",\"cgroup_id\":" << cgid
            << ",\"credits\":" << total_after
            << ",\"delay_ns\":" << slot.delay_ns
            << ",\"count\":" << req.count
            << ",\"source\":\"" << json_escape(source_id) << "\""
            << ",\"source_key\":\"" << format_source_key(source_key) << "\""
            << ",\"source_slot\":" << slot_index
            << ",\"source_credits\":" << slot.credits
            << "}";
        response_json = oss.str();
        return true;
    }

    bool profile_syscalls(const SyscallProfileRequest& req, std::string& response_json) {
        if (req.ns.empty() || req.pod.empty()) {
            response_json = "{\"ok\":false,\"error\":\"namespace and pod are required\"}";
            return false;
        }

        const uint64_t duration_ms = std::clamp<uint64_t>(req.duration_ms, 100, 30000);
        const uint32_t top_k = std::clamp<uint32_t>(req.top_k, 1, 64);

        auto resolved = resolve_target(req.ns, req.pod, req.container);
        if (!resolved.has_value()) {
            response_json = "{\"ok\":false,\"error\":\"failed to resolve cgroup\"}";
            return false;
        }

        uint64_t cgid = resolved->cgroup_id;
        {
            std::lock_guard<std::mutex> lock(mu_);
            if (!active_) {
                response_json = "{\"ok\":false,\"error\":\"request-credit mode is not active\"}";
                return false;
            }
            if (!raw_link_) {
                response_json = "{\"ok\":false,\"error\":\"raw_tracepoint/sys_enter is unavailable\"}";
                return false;
            }
            if (probe_targets_fd_ < 0 || syscall_hist_fd_ < 0) {
                response_json = "{\"ok\":false,\"error\":\"syscall profiling maps are unavailable\"}";
                return false;
            }

            clear_syscall_histogram_locked(cgid);

            uint8_t one = 1;
            if (bpf_map_update_elem(probe_targets_fd_, &cgid, &one, BPF_ANY) != 0) {
                std::ostringstream oss;
                oss << "{\"ok\":false,\"error\":\"failed to enable profile target\",\"errno\":" << errno << "}";
                response_json = oss.str();
                return false;
            }
        }

        const auto end_time = std::chrono::steady_clock::now() + std::chrono::milliseconds(duration_ms);
        while (std::chrono::steady_clock::now() < end_time && running_.load(std::memory_order_acquire) &&
               mcoz_keep_running()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }

        std::vector<std::pair<uint32_t, uint64_t>> rows;
        {
            std::lock_guard<std::mutex> lock(mu_);
            if (probe_targets_fd_ >= 0) {
                bpf_map_delete_elem(probe_targets_fd_, &cgid);
            }
            collect_syscall_histogram_locked(cgid, rows);
        }

        std::sort(rows.begin(), rows.end(), [](const auto& a, const auto& b) {
            if (a.second == b.second) return a.first < b.first;
            return a.second > b.second;
        });
        if (rows.size() > top_k) rows.resize(top_k);

        auto count_of = [&rows](uint32_t id) {
            uint64_t out = 0;
            for (const auto& row : rows) {
                if (row.first == id) out += row.second;
            }
            return out;
        };
        uint64_t recv_total = count_of(45) + count_of(47) + count_of(299);
        uint64_t read_total = count_of(0) + count_of(17) + count_of(19);
        uint64_t io_uring_total = count_of(425) + count_of(426);

        uint32_t recommended_flags = 0;
        if (recv_total > 0) {
            recommended_flags |= (kConsumeRecvfrom | kConsumeRecvmsg | kConsumeRecvmmsg);
        }
        if (read_total > 0) {
            recommended_flags |= (kConsumeRead | kConsumeReadv | kConsumePread64);
        }
        if (io_uring_total > 0) {
            recommended_flags |= kConsumeIoUring;
        }
        if (recommended_flags == 0) {
            recommended_flags = kConsumeDefaultFlags;
        }

        bool apply_ok = true;
        if (req.apply_policy) {
            std::lock_guard<std::mutex> lock(mu_);
            if (consume_policy_fd_ < 0) {
                apply_ok = false;
            } else {
                ConsumePolicyMapValue policy {};
                policy.flags = recommended_flags;
                if (bpf_map_update_elem(consume_policy_fd_, &cgid, &policy, BPF_ANY) != 0) {
                    apply_ok = false;
                }
            }
            if (!apply_ok) {
                std::ostringstream err_oss;
                err_oss << "{\"ok\":false,\"error\":\"failed to apply consume policy\",\"errno\":"
                        << errno << "}";
                response_json = err_oss.str();
                return false;
            }
        }

        std::string dominant = "none";
        if (!rows.empty()) {
            const char* n = syscall_name_from_id(rows.front().first);
            if (std::strcmp(n, "unknown") == 0) {
                dominant = std::string("sys_") + std::to_string(rows.front().first);
            } else {
                dominant = n;
            }
        }

        std::ostringstream oss;
        oss << "{\"ok\":true"
            << ",\"mode\":\"request-credit\""
            << ",\"namespace\":\"" << json_escape(resolved->ns) << "\""
            << ",\"pod\":\"" << json_escape(resolved->pod) << "\""
            << ",\"container\":\"" << json_escape(resolved->container) << "\""
            << ",\"cgroup_id\":" << cgid
            << ",\"duration_ms\":" << duration_ms
            << ",\"top_k\":" << top_k
            << ",\"raw_tracepoint_attached\":" << (raw_link_ ? "true" : "false")
            << ",\"syscalls\":[";

        bool first = true;
        for (const auto& row : rows) {
            if (!first) oss << ",";
            first = false;
            const char* n = syscall_name_from_id(row.first);
            std::string name = (std::strcmp(n, "unknown") == 0)
                                   ? (std::string("sys_") + std::to_string(row.first))
                                   : std::string(n);
            oss << "{"
                << "\"id\":" << row.first
                << ",\"name\":\"" << json_escape(name) << "\""
                << ",\"count\":" << row.second
                << "}";
        }
        oss << "]"
            << ",\"recommendation\":{"
            << "\"dominant\":\"" << json_escape(dominant) << "\""
            << ",\"recv_path_count\":" << recv_total
            << ",\"read_path_count\":" << read_total
            << ",\"io_uring_count\":" << io_uring_total
            << ",\"enable_read_hook\":" << (read_total > 0 ? "true" : "false")
            << ",\"suggested_consume_paths\":" << consume_flags_to_json_array(recommended_flags)
            << ",\"suggested_raw_flags\":" << recommended_flags
            << "}"
            << ",\"apply_policy_requested\":" << (req.apply_policy ? "true" : "false")
            << ",\"apply_policy_ok\":" << (apply_ok ? "true" : "false")
            << ",\"applied_raw_flags\":" << (req.apply_policy ? std::to_string(recommended_flags) : "null")
            << ",\"applied_consume_paths\":"
            << (req.apply_policy ? consume_flags_to_json_array(recommended_flags) : "null")
            << "}";
        response_json = oss.str();
        return true;
    }

    bool apply_consume_policy(const ConsumePolicyApplyRequest& req, std::string& response_json) {
        if (req.ns.empty() || req.pod.empty()) {
            response_json = "{\"ok\":false,\"error\":\"namespace and pod are required\"}";
            return false;
        }

        auto resolved = resolve_target(req.ns, req.pod, req.container);
        if (!resolved.has_value()) {
            response_json = "{\"ok\":false,\"error\":\"failed to resolve cgroup\"}";
            return false;
        }

        const uint64_t cgid = resolved->cgroup_id;
        const uint32_t raw_flags = normalize_consume_flags(req.raw_flags);

        {
            std::lock_guard<std::mutex> lock(mu_);
            if (!active_) {
                response_json = "{\"ok\":false,\"error\":\"request-credit mode is not active\"}";
                return false;
            }
            if (consume_policy_fd_ < 0) {
                response_json = "{\"ok\":false,\"error\":\"consume policy map is unavailable\"}";
                return false;
            }

            ConsumePolicyMapValue policy {};
            policy.flags = raw_flags;
            if (bpf_map_update_elem(consume_policy_fd_, &cgid, &policy, BPF_ANY) != 0) {
                std::ostringstream err_oss;
                err_oss << "{\"ok\":false,\"error\":\"failed to apply consume policy\",\"errno\":"
                        << errno << "}";
                response_json = err_oss.str();
                return false;
            }

            auto& vr = victims_[cgid];
            vr.ns = resolved->ns;
            vr.pod = resolved->pod;
            vr.container = resolved->container;
            vr.cgroup_path = resolved->cgroup_path;
        }

        std::ostringstream oss;
        oss << "{\"ok\":true"
            << ",\"mode\":\"request-credit\""
            << ",\"namespace\":\"" << json_escape(resolved->ns) << "\""
            << ",\"pod\":\"" << json_escape(resolved->pod) << "\""
            << ",\"container\":\"" << json_escape(resolved->container) << "\""
            << ",\"cgroup_id\":" << cgid
            << ",\"apply_policy_ok\":true"
            << ",\"applied_raw_flags\":" << raw_flags
            << ",\"applied_consume_paths\":" << consume_flags_to_json_array(raw_flags)
            << "}";
        response_json = oss.str();
        return true;
    }

    std::string status_json() {
        std::lock_guard<std::mutex> lock(mu_);
        GlobalStatsMapValue bpf_stats {};
        uint32_t key = 0;
        if (global_stats_fd_ >= 0) {
            bpf_map_lookup_elem(global_stats_fd_, &key, &bpf_stats);
        }

        std::ostringstream oss;
        oss << "{\"ok\":true"
            << ",\"mode\":\"request-credit\""
            << ",\"active\":" << (active_ ? "true" : "false")
            << ",\"refund_on_fail\":" << (refund_on_fail_ ? "true" : "false")
            << ",\"default_delay_ns\":" << default_delay_ns_
            << ",\"global\":{"
            << "\"triggered\":" << triggered_.load(std::memory_order_relaxed)
            << ",\"armed_ok\":" << armed_ok_.load(std::memory_order_relaxed)
            << ",\"armed_fail\":" << arm_fail_.load(std::memory_order_relaxed)
            << ",\"consume_trigger\":" << bpf_stats.consume_triggers
            << ",\"bpf_events\":" << bpf_stats.bpf_events
            << ",\"ringbuf_drops\":" << bpf_stats.ringbuf_drops
            << ",\"injected_ok\":" << injected_ok_.load(std::memory_order_relaxed)
            << ",\"inject_fail\":" << inject_fail_.load(std::memory_order_relaxed)
            << ",\"rearm_noop\":" << rearm_noop_.load(std::memory_order_relaxed)
            << "},\"cgroups\":[";

        bool first = true;
        for (auto& it : victims_) {
            uint64_t cgid = it.first;
            VictimRuntime& vr = it.second;
            VictimCfg cfg {};
            if (bpf_map_lookup_elem(victim_map_fd_, &cgid, &cfg) != 0) {
                std::memset(&cfg, 0, sizeof(cfg));
            }
            ConsumePolicyMapValue policy {};
            policy.flags = kConsumeDefaultFlags;
            if (consume_policy_fd_ >= 0) {
                bpf_map_lookup_elem(consume_policy_fd_, &cgid, &policy);
            }
            const int64_t credits_total = total_credits(cfg);
            const uint64_t delay_ns = max_delay(cfg);
            uint64_t miss = 0;
            uint64_t done = vr.injected_ok + vr.inject_fail;
            if (vr.armed_total > done) miss = vr.armed_total - done;

            if (!first) oss << ",";
            first = false;
            oss << "{"
                << "\"cgroup_id\":" << cgid
                << ",\"namespace\":\"" << json_escape(vr.ns) << "\""
                << ",\"pod\":\"" << json_escape(vr.pod) << "\""
                << ",\"container\":\"" << json_escape(vr.container) << "\""
                << ",\"cgroup_path\":\"" << json_escape(vr.cgroup_path) << "\""
                << ",\"delay_ns\":" << delay_ns
                << ",\"credits\":" << credits_total
                << ",\"consume_policy_flags\":" << policy.flags
                << ",\"consume_policy_paths\":" << consume_flags_to_json_array(policy.flags)
                << ",\"arm_calls\":" << vr.arm_calls
                << ",\"armed_total\":" << vr.armed_total
                << ",\"consume_trigger\":" << vr.consume_trigger
                << ",\"bpf_events\":" << vr.bpf_events
                << ",\"injected_ok\":" << vr.injected_ok
                << ",\"inject_fail\":" << vr.inject_fail
                << ",\"refund_ok\":" << vr.refund_ok
                << ",\"refund_fail\":" << vr.refund_fail
                << ",\"miss\":" << miss
                << ",\"last_tid\":" << vr.last_tid
                << ",\"last_ts\":" << vr.last_ts_ns
                << ",\"last_source\":\"" << json_escape(vr.last_source) << "\""
                << ",\"last_source_key\":\"" << format_source_key(vr.last_source_key) << "\""
                << ",\"source_slots_capacity\":" << MCOZ_MAX_SOURCE_SLOTS
                << ",\"sources\":[";

            bool first_source = true;
            std::set<uint64_t> emitted_sources;
            for (const auto& source_it : vr.sources) {
                const auto& sr = source_it.second;
                emitted_sources.insert(sr.source_key);
                int slot_index = find_exact_slot(cfg, sr.source_key);
                const VictimSourceSlot* slot = slot_index >= 0 ? &cfg.slots[slot_index] : nullptr;
                uint64_t source_miss = 0;
                uint64_t source_done = sr.injected_ok + sr.inject_fail;
                if (sr.armed_total > source_done) source_miss = sr.armed_total - source_done;
                if (!first_source) oss << ",";
                first_source = false;
                oss << "{"
                    << "\"source\":\"" << json_escape(sr.source) << "\""
                    << ",\"source_key\":\"" << format_source_key(sr.source_key) << "\""
                    << ",\"slot_index\":";
                if (slot_index >= 0) {
                    oss << slot_index;
                } else {
                    oss << "null";
                }
                oss << ",\"delay_ns\":" << (slot ? slot->delay_ns : 0)
                    << ",\"credits\":" << (slot ? slot->credits : 0)
                    << ",\"arm_calls\":" << sr.arm_calls
                    << ",\"armed_total\":" << sr.armed_total
                    << ",\"consume_trigger\":" << sr.consume_trigger
                    << ",\"bpf_events\":" << sr.bpf_events
                    << ",\"injected_ok\":" << sr.injected_ok
                    << ",\"inject_fail\":" << sr.inject_fail
                    << ",\"miss\":" << source_miss
                    << ",\"last_tid\":" << sr.last_tid
                    << ",\"last_ts\":" << sr.last_ts_ns
                    << "}";
            }
            for (size_t i = 0; i < MCOZ_MAX_SOURCE_SLOTS; ++i) {
                const auto& slot = cfg.slots[i];
                if (slot.source_key == 0 || emitted_sources.count(slot.source_key) != 0) continue;
                if (!first_source) oss << ",";
                first_source = false;
                oss << "{"
                    << "\"source\":\"" << json_escape(format_source_key(slot.source_key)) << "\""
                    << ",\"source_key\":\"" << format_source_key(slot.source_key) << "\""
                    << ",\"slot_index\":" << i
                    << ",\"delay_ns\":" << slot.delay_ns
                    << ",\"credits\":" << slot.credits
                    << ",\"arm_calls\":0"
                    << ",\"armed_total\":0"
                    << ",\"consume_trigger\":0"
                    << ",\"bpf_events\":0"
                    << ",\"injected_ok\":0"
                    << ",\"inject_fail\":0"
                    << ",\"miss\":0"
                    << ",\"last_tid\":0"
                    << ",\"last_ts\":0"
                    << "}";
            }
            oss << "]"
                << "}";
        }
        oss << "]}";
        return oss.str();
    }

    void clear(bool clear_credits) {
        std::lock_guard<std::mutex> lock(mu_);
        for (auto& it : victims_) {
            auto& vr = it.second;
            vr.arm_calls = 0;
            vr.armed_total = 0;
            vr.consume_trigger = 0;
            vr.bpf_events = 0;
            vr.injected_ok = 0;
            vr.inject_fail = 0;
            vr.refund_ok = 0;
            vr.refund_fail = 0;
            vr.last_tid = 0;
            vr.last_ts_ns = 0;
            vr.last_source_key = 0;
            vr.last_source.clear();
            vr.last_error.clear();
            for (auto& source_it : vr.sources) {
                auto& sr = source_it.second;
                sr.arm_calls = 0;
                sr.armed_total = 0;
                sr.consume_trigger = 0;
                sr.bpf_events = 0;
                sr.injected_ok = 0;
                sr.inject_fail = 0;
                sr.last_tid = 0;
                sr.last_ts_ns = 0;
                sr.last_delay_ns = 0;
                sr.last_error.clear();
            }

            if (clear_credits && victim_map_fd_ >= 0) {
                VictimCfg cfg {};
                uint64_t cgid = it.first;
                if (bpf_map_lookup_elem(victim_map_fd_, &cgid, &cfg) == 0) {
                    for (auto& slot : cfg.slots) {
                        slot.credits = 0;
                        slot.delay_ns = 0;
                    }
                    cfg.rr_cursor = 0;
                    bpf_map_update_elem(victim_map_fd_, &cgid, &cfg, BPF_ANY);
                }
            }
        }
        triggered_.store(0, std::memory_order_relaxed);
        armed_ok_.store(0, std::memory_order_relaxed);
        arm_fail_.store(0, std::memory_order_relaxed);
        injected_ok_.store(0, std::memory_order_relaxed);
        inject_fail_.store(0, std::memory_order_relaxed);
        rearm_noop_.store(0, std::memory_order_relaxed);

        if (global_stats_fd_ >= 0) {
            GlobalStatsMapValue zero {};
            uint32_t key = 0;
            bpf_map_update_elem(global_stats_fd_, &key, &zero, BPF_ANY);
        }
    }

    std::string rearm_message() {
        rearm_noop_.fetch_add(1, std::memory_order_relaxed);
        return "request-credit mode: /rearm is a no-op (credits are consumed by recv* triggers)";
    }

  private:
    void clear_syscall_histogram_locked(uint64_t cgid) {
        if (syscall_hist_fd_ < 0) return;
        std::vector<SyscallHistKey> to_delete;
        SyscallHistKey cur {};
        SyscallHistKey next {};
        int rc = bpf_map_get_next_key(syscall_hist_fd_, nullptr, &next);
        while (rc == 0) {
            cur = next;
            if (cur.cgroup_id == cgid) {
                to_delete.push_back(cur);
            }
            rc = bpf_map_get_next_key(syscall_hist_fd_, &cur, &next);
        }
        for (const auto& key : to_delete) {
            bpf_map_delete_elem(syscall_hist_fd_, &key);
        }
    }

    void collect_syscall_histogram_locked(uint64_t cgid, std::vector<std::pair<uint32_t, uint64_t>>& out) {
        out.clear();
        if (syscall_hist_fd_ < 0) return;
        SyscallHistKey cur {};
        SyscallHistKey next {};
        int rc = bpf_map_get_next_key(syscall_hist_fd_, nullptr, &next);
        while (rc == 0) {
            cur = next;
            if (cur.cgroup_id == cgid) {
                uint64_t count = 0;
                if (bpf_map_lookup_elem(syscall_hist_fd_, &cur, &count) == 0 && count > 0) {
                    out.emplace_back(cur.syscall_id, count);
                }
            }
            rc = bpf_map_get_next_key(syscall_hist_fd_, &cur, &next);
        }
    }

    bool consume_credit_locked(uint64_t cgid,
                               uint64_t source_key,
                               uint32_t slot_index_hint,
                               uint64_t& delay_ns_out,
                               int64_t& credits_before_out,
                               int64_t& credits_after_out) {
        if (victim_map_fd_ < 0) return false;
        VictimCfg cfg {};
        if (bpf_map_lookup_elem(victim_map_fd_, &cgid, &cfg) != 0) return false;
        if (!cfg.enabled) return false;
        int slot_index = -1;
        if (slot_index_hint < MCOZ_MAX_SOURCE_SLOTS &&
            cfg.slots[slot_index_hint].source_key == source_key) {
            slot_index = static_cast<int>(slot_index_hint);
        } else {
            slot_index = find_exact_slot(cfg, source_key);
        }
        if (slot_index < 0) return false;
        auto& slot = cfg.slots[slot_index];
        if (slot.delay_ns == 0 || slot.credits <= 0) return false;
        credits_before_out = total_credits(cfg);
        slot.credits -= 1;
        if (bpf_map_update_elem(victim_map_fd_, &cgid, &cfg, BPF_ANY) != 0) return false;
        credits_after_out = total_credits(cfg);
        delay_ns_out = slot.delay_ns;
        return true;
    }

    void stop_unlocked() {
        if (ring_) {
            ring_buffer__free(ring_);
            ring_ = nullptr;
        }
        if (read_link_) {
            bpf_link__destroy(read_link_);
            read_link_ = nullptr;
        }
        if (raw_link_) {
            bpf_link__destroy(raw_link_);
            raw_link_ = nullptr;
        }
        if (recvmsg_link_) {
            bpf_link__destroy(recvmsg_link_);
            recvmsg_link_ = nullptr;
        }
        if (recvfrom_link_) {
            bpf_link__destroy(recvfrom_link_);
            recvfrom_link_ = nullptr;
        }
        if (obj_) {
            bpf_object__close(obj_);
            obj_ = nullptr;
        }
        victim_map_fd_ = -1;
        global_stats_fd_ = -1;
        runtime_flags_fd_ = -1;
        probe_targets_fd_ = -1;
        syscall_hist_fd_ = -1;
        consume_policy_fd_ = -1;
        active_ = false;
    }

    void poll_loop() {
        while (running_.load(std::memory_order_acquire) && mcoz_keep_running()) {
            int rc = ring_buffer__poll(ring_, 200);
            if (rc < 0 && rc != -EINTR) {
                std::cerr << "[WARN] ring_buffer__poll failed: " << rc << "\n";
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
        }
    }

    static int on_ring_event_static(void* ctx, void* data, size_t data_sz) {
        if (!ctx || !data || data_sz < sizeof(ConsumeEvent)) return 0;
        auto* self = reinterpret_cast<RequestCreditEngine*>(ctx);
        const auto* ev = reinterpret_cast<const ConsumeEvent*>(data);
        self->on_ring_event(*ev);
        return 0;
    }

    void on_ring_event(const ConsumeEvent& ev) {
        uint64_t effective_delay_ns = ev.delay_ns;
        int64_t credits_before = 0;
        int64_t credits_after = 0;
        std::string source_desc = format_source_key(ev.source_key);
        {
            std::lock_guard<std::mutex> lock(mu_);
            if (!consume_credit_locked(
                    ev.cgroup_id, ev.source_key, ev.slot_index, effective_delay_ns, credits_before, credits_after)) {
                return;
            }
            auto& vr = victims_[ev.cgroup_id];
            auto source_it = vr.sources.find(ev.source_key);
            if (source_it == vr.sources.end()) {
                auto& sr = vr.sources[ev.source_key];
                sr.source_key = ev.source_key;
                sr.source = format_source_key(ev.source_key);
                source_desc = sr.source;
            } else {
                source_desc = source_it->second.source;
            }
        }

        std::cerr << "[MCOZ] state=CREDIT_CONSUME cgroup_id=" << ev.cgroup_id
                  << " tid=" << ev.tid
                  << " source=" << source_desc
                  << " source_key=" << format_source_key(ev.source_key)
                  << " slot=" << ev.slot_index
                  << " credits=" << credits_before << "->" << credits_after
                  << " delay_ns=" << effective_delay_ns
                  << " syscall_id=" << ev.syscall_id << "\n";

        {
            std::lock_guard<std::mutex> lock(mu_);
            auto& vr = victims_[ev.cgroup_id];
            vr.consume_trigger += 1;
            vr.bpf_events += 1;
            vr.last_tid = ev.tid;
            vr.last_ts_ns = ev.ts_ns;
             vr.last_source_key = ev.source_key;
             vr.last_source = source_desc;
            if (effective_delay_ns > 0) vr.last_delay_ns = effective_delay_ns;
            auto& sr = vr.sources[ev.source_key];
            sr.source_key = ev.source_key;
            if (sr.source.empty()) sr.source = source_desc;
            sr.consume_trigger += 1;
            sr.bpf_events += 1;
            sr.last_tid = ev.tid;
            sr.last_ts_ns = ev.ts_ns;
            if (effective_delay_ns > 0) sr.last_delay_ns = effective_delay_ns;
        }
        std::cerr << "[MCOZ] state=CONSUME_TRIGGER cgroup_id=" << ev.cgroup_id
                  << " tid=" << ev.tid
                  << " source=" << source_desc
                  << " source_key=" << format_source_key(ev.source_key)
                  << " slot=" << ev.slot_index
                  << " delay_ns=" << effective_delay_ns
                  << " syscall_id=" << ev.syscall_id << "\n";

        int err = 0;
        long rc = ghost_delay_tid(static_cast<pid_t>(ev.tid), effective_delay_ns, err);
        if (rc == 0) {
            injected_ok_.fetch_add(1, std::memory_order_relaxed);
            std::lock_guard<std::mutex> lock(mu_);
            auto& vr = victims_[ev.cgroup_id];
            vr.injected_ok += 1;
            auto& sr = vr.sources[ev.source_key];
            sr.source_key = ev.source_key;
            if (sr.source.empty()) sr.source = source_desc;
            sr.injected_ok += 1;
            std::cerr << "[MCOZ] state=INJECTED cgroup_id=" << ev.cgroup_id
                      << " tid=" << ev.tid
                      << " source=" << source_desc
                      << " source_key=" << format_source_key(ev.source_key)
                      << " slot=" << ev.slot_index
                      << " delay_ns=" << effective_delay_ns << "\n";
            return;
        }

        inject_fail_.fetch_add(1, std::memory_order_relaxed);

        std::lock_guard<std::mutex> lock(mu_);
        auto& vr = victims_[ev.cgroup_id];
        vr.inject_fail += 1;
        vr.last_error = std::string("syscall449 failed errno=") + std::to_string(err);
        auto& sr = vr.sources[ev.source_key];
        sr.source_key = ev.source_key;
        if (sr.source.empty()) sr.source = source_desc;
        sr.inject_fail += 1;
        sr.last_error = vr.last_error;
        std::cerr << "[MCOZ] state=FAILED cgroup_id=" << ev.cgroup_id
                  << " tid=" << ev.tid
                  << " source=" << source_desc
                  << " source_key=" << format_source_key(ev.source_key)
                  << " slot=" << ev.slot_index
                  << " delay_ns=" << effective_delay_ns
                  << " errno=" << err
                  << " refunded=0\n";
    }

    std::string resolve_bpf_obj_path() const {
        std::vector<std::string> candidates;
        if (const char* env = std::getenv("MCOZ_REQUEST_BPF_OBJ"); env && *env) {
            candidates.emplace_back(env);
        }
        candidates.emplace_back(MCOZ_REQUEST_BPF_OBJ_PATH);
        candidates.emplace_back("/opt/mcoz/bin/request_credit.bpf.o");
        candidates.emplace_back("/home/yunni/mcoz/mcoz/m-coz/build/hostctl_cpp/request_credit.bpf.o");
        candidates.emplace_back("./build/hostctl_cpp/request_credit.bpf.o");
        for (const auto& c : candidates) {
            if (c.empty()) continue;
            if (access(c.c_str(), R_OK) == 0) return c;
        }
        return "";
    }

    std::optional<std::string> resolve_container_id(const std::string& ns,
                                                    const std::string& pod,
                                                    const std::string& container) {
        std::string cmd = "kubectl get pod " + pod + " -n " + ns +
                          " -o jsonpath='{range .status.containerStatuses[*]}{.name}{\"|\"}{.containerID}{\"\\n\"}{end}' 2>/dev/null";
        FILE* fp = popen(cmd.c_str(), "r");
        if (!fp) return std::nullopt;

        std::string picked;
        std::string fallback;
        char line[1024];
        while (fgets(line, sizeof(line), fp)) {
            std::string s = trim(line);
            if (s.empty()) continue;
            size_t sep = s.find('|');
            if (sep == std::string::npos) continue;
            std::string name = trim(s.substr(0, sep));
            std::string cid = trim(s.substr(sep + 1));
            if (cid.empty() || cid == "null" || cid == "<no value>") continue;
            cid = strip_container_prefix(cid);
            if (cid.empty()) continue;
            if (!container.empty() && name == container) {
                picked = cid;
                break;
            }
            if (fallback.empty() && name != "istio-proxy" && name != "POD") {
                fallback = cid;
            }
            if (fallback.empty()) fallback = cid;
        }
        pclose(fp);

        if (!picked.empty()) return picked;
        if (!fallback.empty()) return fallback;
        return std::nullopt;
    }

    static std::string find_cgroup_by_cid(const std::string& cid) {
        if (cid.empty()) return "";
        std::vector<std::string> roots = {
            "/sys/fs/cgroup",
            "/sys/fs/cgroup/unified",
            "/sys/fs/cgroup/perf_event",
        };
        std::vector<std::string> scan_roots;
        for (const auto& r : roots) {
            if (access(r.c_str(), F_OK) != 0) continue;
            scan_roots.push_back(r);
            std::string k1 = r + "/kubepods.slice";
            std::string k2 = r + "/kubepods";
            std::string k3 = r + "/system.slice";
            if (access(k1.c_str(), F_OK) == 0) scan_roots.push_back(k1);
            if (access(k2.c_str(), F_OK) == 0) scan_roots.push_back(k2);
            if (access(k3.c_str(), F_OK) == 0) scan_roots.push_back(k3);
        }
        auto score_path = [](const std::string& path) -> int {
            int s = 0;
            if (path.rfind("/sys/fs/cgroup/unified/", 0) == 0) s += 200;
            if (path.rfind("/sys/fs/cgroup/kubepods", 0) == 0 ||
                path.rfind("/sys/fs/cgroup/kubepods.slice", 0) == 0) {
                s += 150;
            }
            if (path.find("/kubepods") != std::string::npos) s += 50;
            if (path.rfind("/sys/fs/cgroup/perf_event/", 0) == 0) s += 20;

            static const char* v1_controllers[] = {
                "/sys/fs/cgroup/hugetlb/",
                "/sys/fs/cgroup/memory/",
                "/sys/fs/cgroup/cpu/",
                "/sys/fs/cgroup/cpuacct/",
                "/sys/fs/cgroup/cpuset/",
                "/sys/fs/cgroup/blkio/",
                "/sys/fs/cgroup/pids/",
                "/sys/fs/cgroup/freezer/",
                "/sys/fs/cgroup/net_cls/",
                "/sys/fs/cgroup/net_prio/",
                "/sys/fs/cgroup/devices/",
            };
            for (const char* pref : v1_controllers) {
                if (path.rfind(pref, 0) == 0) {
                    s -= 100;
                    break;
                }
            }
            return s;
        };

        std::string scope_name = "cri-containerd-" + cid + ".scope";
        int best_score = -1000000;
        std::string best_path;
        for (const auto& root : scan_roots) {
            try {
                for (const auto& ent : std::filesystem::recursive_directory_iterator(
                         root, std::filesystem::directory_options::skip_permission_denied)) {
                    if (!ent.is_directory()) continue;
                    const std::string name = ent.path().filename().string();
                    if (name == scope_name || name.find(cid) != std::string::npos) {
                        std::string p = ent.path().string();
                        int score = score_path(p);
                        if (score > best_score) {
                            best_score = score;
                            best_path = std::move(p);
                        }
                    }
                }
            } catch (const std::exception&) {
                continue;
            }
        }
        return best_path;
    }

    std::optional<ResolvedTarget> resolve_target(const std::string& ns,
                                                 const std::string& pod,
                                                 const std::string& container) {
        std::string c = container.empty() ? "app" : container;
        std::string key = ns + "/" + pod + "/" + c;

        {
            std::lock_guard<std::mutex> lock(cache_mu_);
            auto it = cache_.find(key);
            if (it != cache_.end()) {
                auto age = std::chrono::steady_clock::now() - it->second.ts;
                if (age < std::chrono::seconds(30) &&
                    access(it->second.resolved.cgroup_path.c_str(), F_OK) == 0) {
                    return it->second.resolved;
                }
            }
        }

        auto cid_opt = resolve_container_id(ns, pod, c);
        if (!cid_opt.has_value()) return std::nullopt;
        std::string cgroup_path = find_cgroup_by_cid(*cid_opt);
        if (cgroup_path.empty()) return std::nullopt;
        uint64_t cgid = cgroup_id_from_path(cgroup_path);
        if (cgid == 0) return std::nullopt;

        ResolvedTarget out {};
        out.ns = ns;
        out.pod = pod;
        out.container = c;
        out.cgroup_path = cgroup_path;
        out.cgroup_id = cgid;

        {
            std::lock_guard<std::mutex> lock(cache_mu_);
            cache_[key] = CachedTarget {out, std::chrono::steady_clock::now()};
        }
        return out;
    }

    struct CachedTarget {
        ResolvedTarget resolved;
        std::chrono::steady_clock::time_point ts;
    };

    mutable std::mutex mu_;
    std::mutex cache_mu_;
    std::map<std::string, CachedTarget> cache_;
    std::map<uint64_t, VictimRuntime> victims_;

    bpf_object* obj_ = nullptr;
    bpf_link* recvfrom_link_ = nullptr;
    bpf_link* recvmsg_link_ = nullptr;
    bpf_link* raw_link_ = nullptr;
    bpf_link* read_link_ = nullptr;
    ring_buffer* ring_ = nullptr;
    int victim_map_fd_ = -1;
    int global_stats_fd_ = -1;
    int runtime_flags_fd_ = -1;
    int probe_targets_fd_ = -1;
    int syscall_hist_fd_ = -1;
    int consume_policy_fd_ = -1;

    std::atomic<bool> running_ {false};
    std::thread poll_thread_;
    bool active_ = false;

    uint64_t default_delay_ns_ = 10000000ULL;
    bool refund_on_fail_ = true;
    bool enable_read_hook_ = false;

    std::atomic<uint64_t> triggered_ {0};
    std::atomic<uint64_t> armed_ok_ {0};
    std::atomic<uint64_t> arm_fail_ {0};
    std::atomic<uint64_t> injected_ok_ {0};
    std::atomic<uint64_t> inject_fail_ {0};
    std::atomic<uint64_t> rearm_noop_ {0};
};

std::mutex g_engine_mu;
std::unique_ptr<RequestCreditEngine> g_engine;

}  // namespace

bool request_credit_start(uint64_t default_delay_ns,
                          bool refund_on_fail,
                          bool enable_read_hook,
                          std::string& err) {
    std::lock_guard<std::mutex> lock(g_engine_mu);
    if (g_engine && g_engine->is_active()) return true;
    g_engine.reset(new RequestCreditEngine());
    if (!g_engine->start(default_delay_ns, refund_on_fail, enable_read_hook, err)) {
        g_engine.reset();
        return false;
    }
    return true;
}

void request_credit_stop() {
    std::lock_guard<std::mutex> lock(g_engine_mu);
    if (!g_engine) return;
    g_engine->stop();
    g_engine.reset();
}

bool request_credit_is_active() {
    std::lock_guard<std::mutex> lock(g_engine_mu);
    return g_engine && g_engine->is_active();
}

bool request_credit_arm(const RequestArmRequest& req, std::string& response_json) {
    std::lock_guard<std::mutex> lock(g_engine_mu);
    if (!g_engine || !g_engine->is_active()) {
        response_json = "{\"ok\":false,\"error\":\"request-credit mode is not active\"}";
        return false;
    }
    return g_engine->arm(req, response_json);
}

bool request_credit_profile_syscalls(const SyscallProfileRequest& req, std::string& response_json) {
    std::lock_guard<std::mutex> lock(g_engine_mu);
    if (!g_engine || !g_engine->is_active()) {
        response_json = "{\"ok\":false,\"error\":\"request-credit mode is not active\"}";
        return false;
    }
    return g_engine->profile_syscalls(req, response_json);
}

bool request_credit_apply_consume_policy(const ConsumePolicyApplyRequest& req, std::string& response_json) {
    std::lock_guard<std::mutex> lock(g_engine_mu);
    if (!g_engine || !g_engine->is_active()) {
        response_json = "{\"ok\":false,\"error\":\"request-credit mode is not active\"}";
        return false;
    }
    return g_engine->apply_consume_policy(req, response_json);
}

std::string request_credit_status_json() {
    std::lock_guard<std::mutex> lock(g_engine_mu);
    if (!g_engine || !g_engine->is_active()) {
        return "{\"ok\":false,\"mode\":\"request-credit\",\"active\":false}";
    }
    return g_engine->status_json();
}

void request_credit_clear(bool clear_credits) {
    std::lock_guard<std::mutex> lock(g_engine_mu);
    if (!g_engine || !g_engine->is_active()) return;
    g_engine->clear(clear_credits);
}

std::string request_credit_rearm_message() {
    std::lock_guard<std::mutex> lock(g_engine_mu);
    if (!g_engine || !g_engine->is_active()) {
        return "request-credit mode is not active";
    }
    return g_engine->rearm_message();
}

int request_credit_daemon(uint64_t default_delay_ns, bool refund_on_fail, bool enable_read_hook) {
    std::string err;
    if (!request_credit_start(default_delay_ns, refund_on_fail, enable_read_hook, err)) {
        std::cerr << "[ERROR] request-credit start failed: " << err << "\n";
        return 1;
    }

    start_local_control_server();
    while (mcoz_keep_running()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
    request_credit_stop();
    cleanup();
    return 0;
}
