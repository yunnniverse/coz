#ifndef HOSTCTL_PERF_H
#define HOSTCTL_PERF_H
#include <cstdint>
#include <vector>
#include <string>
#include <chrono>

struct cgroup { std::string path; uint64_t id; };

/*  ── 함수 원형 ── */
void init_victims(const std::vector<cgroup>&);
int  perf_sampler_sync(int, std::chrono::milliseconds, double,
                       const std::vector<cgroup>&, const std::string&);
int  perf_sampler_request_aware(int,
                                std::chrono::milliseconds,
                                double,
                                uint64_t,
                                const std::vector<cgroup>&,
                                const std::string&);
int  fixed_delay_injector(std::chrono::milliseconds,
                          uint64_t,
                          const std::vector<cgroup>&);
int  delay_consumer_only(const std::vector<cgroup>&);
int  request_credit_daemon(uint64_t default_delay_ns,
                           bool refund_on_fail,
                           bool enable_read_hook);
size_t prewarm_victims(const std::vector<cgroup>&);
size_t set_victim_pids(const std::vector<pid_t>& pids);
// CRD threads lifecycle
void start_crd_threads();
void stop_crd_threads();
void cleanup();
void sigint_handler(int);          // ← 여기만 남김

// Protect group support: pass resolved cgroups to exclude their active CPUs
void set_protected(const std::vector<cgroup>& protected_cgs);

// Exclude specific CPUs from delay injection (always skipped)
void set_excluded_cpus(const std::vector<int>& cpus);

// Shared control/runtime helpers used by request-credit mode.
void start_local_control_server();
bool mcoz_keep_running();

#endif  // HOSTCTL_PERF_H
