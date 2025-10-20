// perf.cpp – signal-mode with verbose debug
#include "perf.h"

#include <sys/syscall.h>
#include <sys/ioctl.h>
#include <thread>
#include <chrono>
#include <unistd.h>
#include <fcntl.h>
#include <sstream>
#include <pthread.h>   // pthread_setaffinity_np
#include <signal.h>
#include <linux/perf_event.h>
#include <vector>
#include <atomic>
#include <fstream>
#include <iomanip>
#include <ctime>
#include <iostream>
#include <cstring>
#include <sys/time.h>
#include <stdio.h>
#include <stdlib.h>
#include <sched.h>
#include <errno.h>
#include "mcoz_ioctl.h"

/* ──────────────── global state ──────────────── */
static std::vector<int>  g_fds;                 // CPU별 perf FD
static std::atomic<bool> g_running   {true};
static std::atomic<bool> g_paused    {false};
static std::atomic<uint64_t> g_lastNs{0};       // 마지막 활동 시각(ns)
// static int kSigRT = -1;                      // SIGNAL 초기 dummy 값
static struct sigaction sa_alrm;                // freeze-unfreeze alarm용
static std::atomic<uint64_t> g_global_delay{0}; // perf-derived total delay (ns)
static std::atomic<uint64_t> g_crd_delay{0};    // CRD-derived global delay (ns)
static std::atomic<uint64_t> g_pending_delta{0}; // to publish to CRD (ns)

// ---- CRD helpers (use kubectl to avoid external deps) ----
static inline std::string env_or(const char* k, const char* d) {
    const char* v = std::getenv(k); return v ? std::string(v) : std::string(d);
}

struct CRSpec {
    std::string group;
    std::string version;
    std::string plural;
    std::string name;
    std::string ns; // empty for cluster-scoped
};

static CRSpec load_crspec() {
    CRSpec s;
    s.group  = env_or("KUBE_GROUP", "example.com");
    s.version= env_or("KUBE_VERSION", "v1alpha1");
    s.plural = env_or("KUBE_PLURAL", "globaldelays");
    s.name   = env_or("GLOBAL_DELAY_NAME", "global-delay");
    s.ns     = env_or("CR_NAMESPACE", "");
    return s;
}

static std::string res_qualified(const CRSpec& s) {
    return s.plural + std::string(".") + s.group;
}

static std::string ns_flag(const CRSpec& s) {
    return s.ns.empty() || s.ns == "-" ? std::string("") : (std::string(" -n ") + s.ns);
}

static bool kubectl_get_value(const CRSpec& s, uint64_t& out) {
    std::string cmd = std::string("kubectl get ") + res_qualified(s) + " " + s.name + ns_flag(s) +
                      " -o jsonpath='{.spec.value}'";
    FILE* fp = popen(cmd.c_str(), "r");
    if (!fp) return false;
    char buf[128]; std::string val;
    if (fgets(buf, sizeof(buf), fp)) val = buf; pclose(fp);
    // trim
    while (!val.empty() && isspace(val.back())) val.pop_back();
    if (val.empty()) return false;
    try { out = std::stoull(val); return true; } catch (...) { return false; }
}

// Get both spec.value and spec.version with plural fallback
static bool kubectl_get_spec(const CRSpec& s, uint64_t& value_out, uint64_t& version_out) {
    auto try_once = [&](const std::string& plural)->bool{
        CRSpec t = s; t.plural = plural;
        std::string cmd = std::string("kubectl get ") + res_qualified(t) + " " + t.name + ns_flag(t) +
                          " -o jsonpath='{.spec.value} {.spec.version}'";
        FILE* fp = popen(cmd.c_str(), "r");
        if (!fp) return false;
        char buf[256]; std::string line;
        if (fgets(buf, sizeof(buf), fp)) line = buf; pclose(fp);
        while (!line.empty() && isspace(line.back())) line.pop_back();
        if (line.empty()) return false;
        std::istringstream iss(line);
        std::string v1, v2; iss >> v1 >> v2;
        if (v1.empty()) return false;
        try { value_out = std::stoull(v1); } catch (...) { return false; }
        if (!v2.empty()) { try { version_out = std::stoull(v2); } catch (...) { version_out = 0; } }
        else version_out = 0;
        return true;
    };
    if (try_once(s.plural)) return true;
    if (!s.plural.empty() && s.plural.back() == 's') {
        if (try_once(s.plural.substr(0, s.plural.size()-1))) return true;
    } else {
        if (try_once(s.plural + "s")) return true;
    }
    return false;
}

static bool kubectl_patch_value(const CRSpec& s, uint64_t newv) {
    std::string json = std::string("'{\"spec\":{\"value\":" ) + std::to_string(newv) + "}}'";
    std::string cmd = std::string("kubectl patch ") + res_qualified(s) + " " + s.name + ns_flag(s) +
                      " --type merge -p " + json + " >/dev/null 2>&1";
    int rc = system(cmd.c_str());
    return (rc == 0);
}

// Patch both value and version fields
static bool kubectl_patch_spec(const CRSpec& s, uint64_t new_value, uint64_t new_version) {
    std::string json = std::string("'{\"spec\":{\"value\":") + std::to_string(new_value) +
                      ",\"version\":" + std::to_string(new_version) + "}}'";
    std::string cmd = std::string("kubectl patch ") + res_qualified(s) + " " + s.name + ns_flag(s) +
                      " --type merge -p " + json + " >/dev/null 2>&1";
    int rc = system(cmd.c_str());
    return (rc == 0);
}

static std::thread th_watch, th_update;

static void crd_watch_thread() {
    CRSpec s = load_crspec();
    uint64_t last = g_crd_delay.load(std::memory_order_relaxed);
    std::cerr << "[coz-daemon] CRD watch start: resource=" << res_qualified(s)
              << " name=" << s.name
              << (s.ns.empty() || s.ns == "-" ? " (cluster-scoped)" : (" ns=" + s.ns))
              << "\n";
    while (g_running.load()) {
        uint64_t v=0, ver=0;
        if (kubectl_get_spec(s, v, ver)) {
            if (v != last) {
                g_crd_delay.store(v, std::memory_order_release);
                // Catch: observed CRD change
                std::cerr << "[coz-daemon] Catch | value=" << v
                          << " ns version=" << ver << "\n";
                last = v;
            }
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
}

static void crd_update_thread() {
    CRSpec s = load_crspec();
    // simple cache of last known value to avoid an extra GET on every small delta
    uint64_t cached_val = 0, cached_ver = 0;
    kubectl_get_spec(s, cached_val, cached_ver);
    while (g_running.load()) {
        uint64_t delta = g_pending_delta.exchange(0, std::memory_order_acq_rel);
        if (delta == 0) { std::this_thread::sleep_for(std::chrono::milliseconds(50)); continue; }
        // ensure current
        uint64_t cur_val = 0, cur_ver = 0;
        if (!kubectl_get_spec(s, cur_val, cur_ver)) { cur_val = cached_val; cur_ver = cached_ver; }
        uint64_t next_val = cur_val + delta;
        uint64_t next_ver = cur_ver + 1; // bump version by one per update
        if (kubectl_patch_spec(s, next_val, next_ver)) {
            cached_val = next_val; cached_ver = next_ver;
            g_crd_delay.store(next_val, std::memory_order_release);
            std::cerr << "[coz-daemon] CRD patched: value=" << next_val << " version=" << next_ver << "\n";
        } else {
            // requeue and backoff
            g_pending_delta.fetch_add(delta, std::memory_order_relaxed);
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        }
    }
}

void start_crd_threads() {
    if (!th_watch.joinable()) th_watch = std::thread(crd_watch_thread);
    if (!th_update.joinable()) th_update = std::thread(crd_update_thread);
}

void stop_crd_threads() {
    g_running.store(false);
    if (th_watch.joinable()) th_watch.join();
    if (th_update.joinable()) th_update.join();
}

static inline void report_increment(uint64_t ns) {
    g_pending_delta.fetch_add(ns, std::memory_order_relaxed);
}

struct cg_info { std::string path; int fd; };
static std::vector<cg_info> g_victims;
static bool is_cgv2 = (access("/sys/fs/cgroup/cgroup.controllers",F_OK)==0);

static long perf_event_open(struct perf_event_attr* attr,int pid,int cpu,int grp,unsigned long flags){
    long r = syscall(__NR_perf_event_open, attr, pid, cpu, grp, flags);
    if (r == -1) {
        int err = errno;
        std::cerr << "[ERROR] perf_event_open failed: errno=" << err
                  << " (" << strerror(err) << ")"
                  << " attr.size=" << attr->size
                  << " type=" << attr->type
                  << " config=" << attr->config
                  << " sample_period=" << attr->sample_period
                  << " disabled=" << (int)attr->disabled
                  << " read_fmt=0x" << std::hex << attr->read_format << std::dec
                  << " exclude_idle=" << (int)attr->exclude_idle
                  << " pid/cgroup_fd=" << pid
                  << " cpu=" << cpu
                  << " group_fd=" << grp
                  << " flags=0x" << std::hex << flags << std::dec
                  << "\n";
        // keep legacy perror line for quick grepping
        perror("perf_event_open");
        return -1; // ensure callers see the failure
    }
    printf("[INFO] perf_event_open success: fd=%ld\n", r);
    return r;
}

static long mcoz_sleep(uint64_t ns){
    // syscall number : 449
    long r=syscall(449, ns);
    if(r!=0) perror("mcoz_delay");
}

static std::vector<int> online_cpus(){
    int n=sysconf(_SC_NPROCESSORS_ONLN); std::vector<int> v; for(int i=0;i<n;++i) v.push_back(i);
    std::cerr << "[DBG] online cpu cnt="<<v.size()<<"\n"; return v; }

/* ────── SCOZ version: per‑core handler ────── */
struct CoreHandler {
    int cpu;                    // CPU 코어
    uint64_t local_delay{0};    // local delay
    std::thread th;
    // int fd;
    // uint64_t prev {0};
};
static std::vector<CoreHandler> g_handlers;

/* - debug용 - */
double seconds_since_boot() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts); // 부팅 후 시간
    return ts.tv_sec + ts.tv_nsec / 1e9;
}


/* ───────────── sampler entry ───────────── */
int perf_sampler_sync(int cg_fd,
                      std::chrono::milliseconds period,
                      double speedup,
                      const std::vector<cgroup>& others,
                      const std::string& /*mode*/)
{
    std::cerr << "[INFO] sampler start (per‑core)\n";

    /* CPU 갯수 불러옴 */
    auto cpu_cnt = sysconf(_SC_NPROCESSORS_ONLN);
    /* 각 CPU 코어별로 */
    g_handlers.reserve(cpu_cnt);

    for (int cpu = 0; cpu < cpu_cnt; ++cpu) {
        // Create handler in-place and work with a reference to the last element
        g_handlers.emplace_back(CoreHandler{cpu});
        // std::cout << "[Info] Percentage = " << speedup << "%" << std::endl;
        CoreHandler& h = g_handlers.back();
        h.cpu = cpu;
        h.local_delay = 0;

        h.th = std::thread([&, cg_fd, period, speedup] {

            /* ── (선택) 스레드‑CPU 바인딩 ── */
            cpu_set_t set; CPU_ZERO(&set); CPU_SET(h.cpu, &set);
            pthread_setaffinity_np(pthread_self(), sizeof(set), &set);

            /* ────── Set ioctl ────── */
            int ioctl_fd = open("/dev/mcoz", O_RDWR);
            if (ioctl_fd < 0) { perror("open /dev/mcoz"); return; }
            // else {
            //     printf("[Core %d] /dev/mcoz opened: fd=%d\n", h.cpu, ioctl_fd);
            // } 확인완료
            /* ───────────────── */

            /* ── 1. perf_event_open ── */
            perf_event_attr pe{}; pe.size = sizeof(pe);
            pe.type            = PERF_TYPE_SOFTWARE;
            // Use CPU clock for cgroup+per-CPU accounting; TASK_CLOCK is per-task and
            // may not be supported with PERF_FLAG_PID_CGROUP on some kernels.
            pe.config          = PERF_COUNT_SW_CPU_CLOCK;
            pe.sample_period   = 0;                            // 샘플링 이벤트 없음 -> event_based가 아님
            pe.disabled        = 1;
            pe.read_format     = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
            pe.exclude_idle    = 1;

            int fd = perf_event_open(&pe, cg_fd, h.cpu, -1, PERF_FLAG_PID_CGROUP | PERF_FLAG_FD_CLOEXEC);
            if (fd < 0) {
                // Retry with a hardware event as a fallback (e.g., CPU cycles)
                pe.type = PERF_TYPE_SOFTWARE;
                pe.config = PERF_COUNT_HW_CPU_CYCLES;
                fd = perf_event_open(&pe, cg_fd, h.cpu, -1, PERF_FLAG_PID_CGROUP | PERF_FLAG_FD_CLOEXEC);
            }
            if (fd < 0) {
                perror("perf_event_open - error");
                if (!is_cgv2 && access("/sys/fs/cgroup/perf_event", F_OK) != 0) {
                    std::cerr << "[ERROR] cgroup v1 perf_event controller not mounted at /sys/fs/cgroup/perf_event.\n"
                              << "        Try: mount -t cgroup -o perf_event cgroup /sys/fs/cgroup/perf_event\n";
                }
                close(ioctl_fd);
                return;
            }
            // std::cout << "[Core " << cpu << "] perf fd : " << fd << std::endl;

            if (fcntl(fd, F_SETOWN, getpid()) == -1) perror("F_SETOWN");
            int fl = fcntl(fd, F_GETFL,0);
            if (fcntl(fd, F_SETFL, fl | O_NONBLOCK | O_ASYNC) == -1) perror("F_SETFL");

            // ioctl(fd,PERF_EVENT_IOC_REFRESH,1); 
            ioctl(fd,PERF_EVENT_IOC_ENABLE,0);
            ioctl(fd,PERF_EVENT_IOC_RESET, 0 );
            
            uint64_t prev = 0, buf[3]{};

            /* ── 2. 루프를 통해 주기적으로 read ── */
            while (g_running.load()) {
                std::this_thread::sleep_for(std::chrono::milliseconds(100)); // 일단 1.02~1.67ms 정도

                /* // ── (추가) 스레드‑CPU 바인딩 유지 검사 ──
                int cur_cpu = sched_getcpu();              // 현재 실행 중인 CPU
                if (cur_cpu != cpu) {
                    cpu_set_t re; CPU_ZERO(&re); CPU_SET(cpu, &re);
                    if (pthread_setaffinity_np(pthread_self(),
                                            sizeof(re), &re) == 0) {
                        std::cerr << "[Core " << cpu
                                << "] re‑affinitized (was on CPU "
                                << cur_cpu << ")\n";
                    } else {
                        perror("pthread_setaffinity_np");
                    }
                }
                */

                // std::cout << "==================================" << std::endl;
                if (read(fd, buf, sizeof(buf)) != sizeof(buf)) {
                    perror("perf read"); break;
                }
                uint64_t delta = buf[0] - prev;
                prev = buf[0];
                if (delta) { 
                    std::cout << "[Core " << h.cpu << "] Target Time : " << delta << "\n"; 
                    
                    /* ── delta > 0 : delay count 증가 ── */
                    uint64_t delay_ns = static_cast<uint64_t>(delta * speedup); // 시간 * 비율
                    h.local_delay += delay_ns;
                    g_global_delay.fetch_add(delay_ns, std::memory_order_relaxed);
                    report_increment(delay_ns);
                    
                    uint64_t global = g_global_delay.load(std::memory_order_acquire);
                }

                for (int i = 0; i < 1; ++i) { // 최대 4회 등 안전 상한 -> 그냥 무식하게 1회 
                    uint64_t global_perf = g_global_delay.load(std::memory_order_acquire);
                    uint64_t global = std::max(global_perf, g_crd_delay.load(std::memory_order_acquire));
                    uint64_t local  = h.local_delay;

                    if (global <= local) break;
                    // std::cout << "[Core " << h.cpu << "] global : " << global << " local : " << local << std::endl;

                    uint64_t consume = global - local;

                    // 너무 길게 재우지 않도록 cap (예: 200µs)
                    // const uint64_t kCapNs = 200'000; // 필요 시 조정
                    // uint64_t consume = (diff > kCapNs) ? kCapNs : diff;
                    
                    // 이거 시간 확인하는 용도
                    // double t = seconds_since_boot();
                    // std::cout << std::fixed << std::setprecision(6); // ← 핵심!
                    // std::cout << "[Core " << h.cpu << "] Delay of " << consume << " ns" << std::endl;

                    // timespec ts;
                    // clock_gettime(CLOCK_MONOTONIC, &ts);
                    // uint64_t user_before = ts.tv_sec*1000000000ULL + ts.tv_nsec;

                    // uint64_t kernel_done = mcoz_sleep(consume); // 커널에서 ktime_get_ns() 반환
                    /* ── ioctl text ── */
                    struct mcoz_delay_req req;
                    req.ns = consume;
                    req.flags = 1;
                    if (ioctl(ioctl_fd, MCOZ_IOC_DELAY, &req) != 0){
                        int err = errno;
                        perror("ioctl MCOZ_IOC_DELAY");
                        if (err == ENOTTY) {
                            std::cerr << "[ERROR] /dev/mcoz ENOTTY (ABI mismatch?). Stopping sampler on core "
                                      << h.cpu << "\n";
                            g_running.store(false, std::memory_order_release);
                            return; // exit thread
                        }
                        // reopen device on transient errors
                        close(ioctl_fd);
                        for (int attempt = 0; attempt < 5; ++attempt) {
                            ioctl_fd = open("/dev/mcoz", O_RDWR);
                            if (ioctl_fd >= 0) break;
                            std::this_thread::sleep_for(std::chrono::milliseconds(100));
                        }
                        if (ioctl_fd < 0) {
                            std::this_thread::sleep_for(std::chrono::milliseconds(200));
                        }
                        break;
                    }
                    // Update: applied delay on this core
                    std::cerr << "[coz-daemon] Update | [core " << h.cpu << "] consume="
                              << consume << " ns\n";
                    /* ── ioctl text ── */

                    // clock_gettime(CLOCK_MONOTONIC, &ts);
                    // uint64_t user_after = ts.tv_sec*1000000000ULL + ts.tv_nsec;
                   
                    // if (h.cpu == 7){
                    //     std::cout << std::fixed << std::setprecision(6)
                    //         << "[Core " << h.cpu << "]"
                    //         << " " << consume << "\n";
                    //         // << "[" << user_before/1e9 << "] ~"
                    //         // << "["  << user_after /1e9 << "]\n";
                    // }

                    h.local_delay += consume;
                }

            }

            ioctl(fd, PERF_EVENT_IOC_DISABLE, 0);
            close(fd);
        });

        // std::cerr << "[DBG] handler thread for cpu=" << cpu << " launched\n";
    } 

    /* ─── cleanup ─── */
    std::cerr << "[INFO] sampler stopping…\n";
    for (auto& h : g_handlers) {
        if (h.th.joinable()) h.th.join();
    }
    cleanup();
    return 0;
}

/* ───────────── cleanup & sigint ───────────── */
void cleanup(){
    std::cerr << "[INFO] cleanup\n";
    for(auto& v:g_victims) close(v.fd);
    }

void sigint_handler(int){ g_running=false; }

/* ───────────── alternate consumer‑only mode ───────────── */
int delay_consumer_only(double /*speedup_unused*/)
{
    std::cerr << "[INFO] consumer-only mode (CRD-driven delays)\n";
    auto cpu_cnt = sysconf(_SC_NPROCESSORS_ONLN);
    g_handlers.reserve(cpu_cnt);

    start_crd_threads();

    for (int cpu = 0; cpu < cpu_cnt; ++cpu) {
        g_handlers.emplace_back(CoreHandler{cpu});
        CoreHandler& h = g_handlers.back();
        h.cpu = cpu;
        h.local_delay = 0;

        h.th = std::thread([&]{
            cpu_set_t set; CPU_ZERO(&set); CPU_SET(h.cpu, &set);
            pthread_setaffinity_np(pthread_self(), sizeof(set), &set);

            int ioctl_fd = open("/dev/mcoz", O_RDWR);
            if (ioctl_fd < 0) { perror("open /dev/mcoz"); return; }

            while (g_running.load()) {
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
                for (int i = 0; i < 1; ++i) {
                    uint64_t global = g_crd_delay.load(std::memory_order_acquire);
                    uint64_t local  = h.local_delay;
                    if (global <= local) break;
                    uint64_t consume = global - local;
                    std::cerr << "[coz-daemon] Update | [core " << h.cpu << "] consume="
                              << consume << " ns (global=" << global
                              << " local=" << local << ")\n";
                    struct mcoz_delay_req req{.ns=consume, .flags=1};
                    if (ioctl(ioctl_fd, MCOZ_IOC_DELAY, &req) != 0){
                        perror("ioctl MCOZ_IOC_DELAY file error in delay_consumer_only");
                        close(ioctl_fd);
                        return;
                    }
                    h.local_delay += consume;
                }
            }
            close(ioctl_fd);
        });
    }

    for (auto& h : g_handlers) if (h.th.joinable()) h.th.join();
    stop_crd_threads();
    cleanup();
    return 0;
}
