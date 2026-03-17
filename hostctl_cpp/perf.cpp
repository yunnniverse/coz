// perf.cpp – signal-mode with verbose debug
#include "perf.h"
#include "request_credit.h"

#include <sys/syscall.h>
#include <sys/ioctl.h>
#include <thread>
#include <chrono>
#include <unistd.h>
#include <fcntl.h>
#include <sstream>
#include <signal.h>
#include <linux/perf_event.h>
#include <vector>
#include <atomic>
#include <fstream>
#include <iomanip>
#include <ctime>
#include <iostream>
#include <cstring>
#include <cctype>
#include <filesystem>
#include <algorithm>
#include <netdb.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <endian.h>
#include <mutex>
#include <sys/time.h>
#include <stdio.h>
#include <stdlib.h>
#include <errno.h>
#include <map>

/* ──────────────── global state ──────────────── */
static std::vector<int>  g_fds;                 // CPU별 perf FD
static std::atomic<bool> g_running   {true};
static std::atomic<bool> g_paused    {false};
static std::atomic<uint64_t> g_lastNs{0};       // 마지막 활동 시각(ns)
// static int kSigRT = -1;                      // SIGNAL 초기 dummy 값
static struct sigaction sa_alrm;                // freeze-unfreeze alarm용
static std::atomic<uint64_t> g_crd_delay{0};    // CRD-derived global delay (ns)
static std::atomic<uint64_t> g_pending_delta{0}; // to publish to CRD (ns)
static std::atomic<uint64_t> g_accumulated_delay_ns{0}; // sampled delay sum for reporting/clear (ns)
static std::atomic<bool> g_clear_server_started{false}; // guard clear HTTP server init
static std::atomic<bool> g_arm_udp_server_started{false};
static std::atomic<bool> g_request_armed{true}; // request-aware mode: inject at most once until rearmed
static std::atomic<uint64_t> g_rearm_immediate_delay_ns{0}; // if >0, /rearm injects this delay once immediately

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
    // quiet
    while (g_running.load()) {
        uint64_t v=0, ver=0;
        if (kubectl_get_spec(s, v, ver)) {
            if (v != last) {
                g_crd_delay.store(v, std::memory_order_release);
                // Catch: observed CRD change
                // quiet
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
            // quiet
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

struct cg_info { std::string path; int fd; };
static std::vector<cg_info> g_victims;
static bool is_cgv2 = (access("/sys/fs/cgroup/cgroup.controllers",F_OK)==0);

/* ───────────── Protect group (exclude active CPUs) ───────────── */
static std::vector<int> g_protect_cg_fds;                       // cgroup dir fds
static std::vector<std::vector<int>> g_protect_perf_fds;        // [cpu][k]
static std::vector<std::vector<uint64_t>> g_protect_prev_vals;  // [cpu][k]
static std::vector<std::vector<int>> g_smt_siblings;            // [cpu] => siblings (including self)

static std::vector<int> parse_siblings(int cpu){
    std::vector<int> sibs;
    char path[256]; snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%d/topology/thread_siblings_list", cpu);
    std::ifstream f(path);
    if(!f.good()) { sibs.push_back(cpu); return sibs; }
    std::string s; std::getline(f,s);
    // format e.g. "0-1,8-9" or "3"
    size_t i=0;
    while(i<s.size()){
        while(i<s.size() && (s[i]==','||s[i]==' ')) ++i;
        if(i>=s.size()) break;
        int a=0,b=0; bool range=false; int val=0; size_t j=i;
        while(j<s.size() && isdigit(s[j])){ val = val*10 + (s[j]-'0'); ++j; }
        a = val; i=j;
        if(i<s.size() && s[i]=='-'){
            ++i; range=true; val=0; j=i; while(j<s.size() && isdigit(s[j])){ val = val*10 + (s[j]-'0'); ++j; }
            b = val; i=j;
        }
        if(range){ for(int c=a;c<=b;++c) sibs.push_back(c); }
        else { sibs.push_back(a); }
    }
    if(sibs.empty()) sibs.push_back(cpu);
    return sibs;
}

// forward declaration for use before definition
static long perf_event_open(struct perf_event_attr* attr,int pid,int cpu,int grp,unsigned long flags);

static void init_protect_watchers(int cpu_cnt){
    g_smt_siblings.resize(cpu_cnt);
    for(int c=0;c<cpu_cnt;++c) g_smt_siblings[c] = parse_siblings(c);

    if (g_protect_cg_fds.empty()) return; // nothing to watch
    g_protect_perf_fds.assign(cpu_cnt, {});
    g_protect_prev_vals.assign(cpu_cnt, {});

    for (int cpu = 0; cpu < cpu_cnt; ++cpu) {
        std::vector<int> fds; fds.reserve(g_protect_cg_fds.size());
        std::vector<uint64_t> prev; prev.reserve(g_protect_cg_fds.size());

        for (int cgfd : g_protect_cg_fds) {
            perf_event_attr pe{}; pe.size = sizeof(pe);
            pe.type = PERF_TYPE_SOFTWARE;
            pe.config = PERF_COUNT_SW_CPU_CLOCK;
            pe.sample_period = 0;
            pe.disabled = 1;
            pe.read_format = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
            pe.exclude_idle = 1;

            int fd = perf_event_open(&pe, cgfd, cpu, -1, PERF_FLAG_PID_CGROUP | PERF_FLAG_FD_CLOEXEC);
            if (fd >= 0) {
                ioctl(fd, PERF_EVENT_IOC_ENABLE, 0);
                ioctl(fd, PERF_EVENT_IOC_RESET, 0);
                fds.push_back(fd);
                prev.push_back(0);
            } else {
                // keep placeholders aligned
                fds.push_back(-1);
                prev.push_back(0);
            }
        }
        g_protect_perf_fds[cpu] = std::move(fds);
        g_protect_prev_vals[cpu] = std::move(prev);
    }
}

/* ───────────── Static CPU exclusion (skip injection on these CPUs) ───────────── */
static std::vector<char> g_exclude_bitmap; // 1 = skip
void set_excluded_cpus(const std::vector<int>& cpus){
    int n = sysconf(_SC_NPROCESSORS_ONLN);
    g_exclude_bitmap.assign(n, 0);
    for (int c : cpus) if (0 <= c && c < n) g_exclude_bitmap[c] = 1;
}

void set_protected(const std::vector<cgroup>& protected_cgs){
    // close previous
    for (auto& per_cpu : g_protect_perf_fds) {
        for (int fd : per_cpu) if (fd >= 0) close(fd);
    }
    g_protect_perf_fds.clear();
    g_protect_prev_vals.clear();
    for (int fd : g_protect_cg_fds) if (fd >= 0) close(fd);
    g_protect_cg_fds.clear();

    for (const auto& cg : protected_cgs) {
        int fd = open(cg.path.c_str(), O_DIRECTORY);
        if (fd >= 0) g_protect_cg_fds.push_back(fd);
    }
    int cpu_cnt = sysconf(_SC_NPROCESSORS_ONLN);
    init_protect_watchers(cpu_cnt);
}

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
    // printf("[INFO] perf_event_open success: fd=%ld\n", r);
    return r;
}

static long ghost_delay(pid_t pid, uint64_t ns){
    // syscall number : 449
    // 우리 syscall 번호
    std::cerr << "[DBG] ghost_delay call: pid=" << pid << " ns=" << ns << "\n";
    long r = syscall(449, pid, ns);
    if(r!=0) {
        int err = errno;
        std::cerr << "[ERR] ghost_delay failed: pid=" << pid
                  << " ns=" << ns
                  << " errno=" << err
                  << " (" << strerror(err) << ")\n";
    }
    return r;
}

static std::vector<pid_t> read_cgroup_tasks(int cg_fd) {
    std::vector<pid_t> pids;
    if (cg_fd < 0) return pids;

    int fd = openat(cg_fd, "tasks", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return pids;

    std::string data;
    char buf[4096];
    ssize_t n = 0;
    while ((n = read(fd, buf, sizeof(buf))) > 0) data.append(buf, n);
    close(fd);
    if (data.empty()) return pids;

    std::istringstream iss(data);
    long v = 0;
    while (iss >> v) {
        if (v > 0) pids.push_back(static_cast<pid_t>(v));
    }
    return pids;
}

static void collect_pids_from_path(const std::string& path, std::vector<pid_t>& out) {
    int fd = open(path.c_str(), O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) return;
    auto part = read_cgroup_tasks(fd);
    close(fd);
    out.insert(out.end(), part.begin(), part.end());
}

static void collect_pids_from_tree(const std::string& root, std::vector<pid_t>& out) {
    collect_pids_from_path(root, out);
    try {
        for (const auto& dir : std::filesystem::recursive_directory_iterator(
                 root, std::filesystem::directory_options::skip_permission_denied)) {
            if (!dir.is_directory()) continue;
            collect_pids_from_path(dir.path().string(), out);
        }
    } catch (const std::exception&) {
        // skip errors and continue
    }
}

static bool apply_delay_to_pids(int cg_fd, uint64_t ns) {
    auto pids = read_cgroup_tasks(cg_fd);
    if (pids.empty()) return false;
    bool any = false;
    for (size_t i = 0; i < pids.size(); ++i) {
        if (ns == 0) continue;
        ghost_delay(pids[i], ns);
        any = true;
    }
    return any;
}

static std::vector<pid_t> collect_pids_from_cgroups(const std::vector<cgroup>& cgs) {
    std::vector<pid_t> pids;
    for (const auto& cg : cgs) {
        collect_pids_from_tree(cg.path, pids);
    }
    if (!pids.empty()) {
        std::sort(pids.begin(), pids.end());
        pids.erase(std::unique(pids.begin(), pids.end()), pids.end());
    }
    return pids;
}

static std::mutex g_victim_mu;
static std::vector<pid_t> g_victim_pids;
static std::chrono::steady_clock::time_point g_victim_ts;

size_t prewarm_victims(const std::vector<cgroup>& victims) {
    auto pids = collect_pids_from_cgroups(victims);
    std::lock_guard<std::mutex> lock(g_victim_mu);
    g_victim_pids = std::move(pids);
    g_victim_ts = std::chrono::steady_clock::now();
    return g_victim_pids.size();
}

size_t set_victim_pids(const std::vector<pid_t>& pids) {
    std::vector<pid_t> filtered;
    filtered.reserve(pids.size());
    for (auto pid : pids) {
        if (pid > 0) filtered.push_back(pid);
    }
    if (!filtered.empty()) {
        std::sort(filtered.begin(), filtered.end());
        filtered.erase(std::unique(filtered.begin(), filtered.end()), filtered.end());
    }
    std::lock_guard<std::mutex> lock(g_victim_mu);
    g_victim_pids = std::move(filtered);
    g_victim_ts = std::chrono::steady_clock::now();
    return g_victim_pids.size();
}

static std::vector<pid_t> get_cached_victim_pids(const std::vector<cgroup>&) {
    std::lock_guard<std::mutex> lock(g_victim_mu);
    return g_victim_pids;
}

static bool apply_delay_to_cgroups(const std::vector<cgroup>& cgs, uint64_t ns) {
    auto pids = get_cached_victim_pids(cgs);
    if (pids.empty()) return false;
    bool any = false;
    for (size_t i = 0; i < pids.size(); ++i) {
        if (ns == 0) continue;
        ghost_delay(pids[i], ns);
        any = true;
    }
    return any;
}

struct DelayNetConfig {
    std::string service;
    int port;
    std::string self_ip;
};

static DelayNetConfig load_delay_net_config() {
    DelayNetConfig cfg;
    cfg.service = env_or("DELAY_SERVICE", "coz-delay");
    cfg.self_ip = env_or("POD_IP", "");
    std::string port_s = env_or("DELAY_PORT", "9808");
    try {
        cfg.port = std::stoi(port_s);
    } catch (...) {
        cfg.port = 0;
    }
    return cfg;
}

static std::mutex g_peer_mu;
static std::vector<sockaddr_in> g_peer_cache;
static std::chrono::steady_clock::time_point g_peer_cache_ts;

static std::vector<sockaddr_in> resolve_peers(const DelayNetConfig& cfg, bool include_self) {
    std::lock_guard<std::mutex> lock(g_peer_mu);
    auto now = std::chrono::steady_clock::now();
    if (!g_peer_cache.empty() &&
        (now - g_peer_cache_ts) < std::chrono::seconds(1)) {
        return g_peer_cache;
    }

    g_peer_cache.clear();
    if (cfg.service.empty() || cfg.port <= 0) return g_peer_cache;

    addrinfo hints{};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_DGRAM;
    addrinfo* res = nullptr;
    if (getaddrinfo(cfg.service.c_str(), nullptr, &hints, &res) != 0) {
        return g_peer_cache;
    }
    for (addrinfo* ai = res; ai; ai = ai->ai_next) {
        if (!ai->ai_addr || ai->ai_addrlen < sizeof(sockaddr_in)) continue;
        sockaddr_in addr = *reinterpret_cast<sockaddr_in*>(ai->ai_addr);
        addr.sin_port = htons(cfg.port);
        char ipbuf[INET_ADDRSTRLEN]{};
        const char* ip = inet_ntop(AF_INET, &addr.sin_addr, ipbuf, sizeof(ipbuf));
        if (!include_self && ip && !cfg.self_ip.empty() && cfg.self_ip == ip) continue;
        g_peer_cache.push_back(addr);
    }
    freeaddrinfo(res);
    g_peer_cache_ts = now;
    return g_peer_cache;
}

static bool send_delay_to_peers(uint64_t ns, bool include_self) {
    DelayNetConfig cfg = load_delay_net_config();
    auto peers = resolve_peers(cfg, include_self);
    if (peers.empty()) return false;
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) return false;
    uint64_t payload = htobe64(ns);
    bool sent = false;
    for (const auto& addr : peers) {
        if (sendto(sock, &payload, sizeof(payload), 0,
                   reinterpret_cast<const sockaddr*>(&addr), sizeof(addr)) >= 0) {
            sent = true;
        }
    }
    close(sock);
    return sent;
}

static int load_clear_http_port() {
    std::string port_s = env_or("CLEAR_HTTP_PORT", "19090");
    try {
        int port = std::stoi(port_s);
        if (port <= 0 || port > 65535) return 0;
        return port;
    } catch (...) {
        return 0;
    }
}

static int load_arm_udp_port() {
    std::string port_s = env_or("ARM_UDP_PORT", "");
    if (port_s.empty()) return load_clear_http_port();
    try {
        int port = std::stoi(port_s);
        if (port <= 0 || port > 65535) return 0;
        return port;
    } catch (...) {
        return 0;
    }
}

static bool parse_http_method_path(const std::string& req, std::string& method, std::string& path) {
    std::istringstream iss(req);
    iss >> method >> path;
    if (method.empty() || path.empty()) return false;
    if (!(method == "GET" || method == "POST")) return false;
    return true;
}

static std::string trim_copy(const std::string& s) {
    if (s.empty()) return s;
    size_t b = s.find_first_not_of(" \n\r\t");
    if (b == std::string::npos) return "";
    size_t e = s.find_last_not_of(" \n\r\t");
    return s.substr(b, e - b + 1);
}

static bool recv_http_request(int fd, std::string& out) {
    out.clear();
    constexpr size_t kMaxReqBytes = 64 * 1024;
    char buf[4096];
    while (out.size() < kMaxReqBytes) {
        ssize_t n = recv(fd, buf, sizeof(buf), 0);
        if (n <= 0) break;
        out.append(buf, static_cast<size_t>(n));

        size_t hdr_end = out.find("\r\n\r\n");
        if (hdr_end == std::string::npos) continue;

        size_t content_length = 0;
        size_t line_start = 0;
        while (line_start < hdr_end) {
            size_t line_end = out.find("\r\n", line_start);
            if (line_end == std::string::npos || line_end > hdr_end) break;
            std::string line = out.substr(line_start, line_end - line_start);
            auto colon = line.find(':');
            if (colon != std::string::npos) {
                std::string key = line.substr(0, colon);
                std::transform(key.begin(), key.end(), key.begin(), [](unsigned char c) {
                    return static_cast<char>(std::tolower(c));
                });
                if (key == "content-length") {
                    std::string v = line.substr(colon + 1);
                    v = trim_copy(v);
                    char* end = nullptr;
                    errno = 0;
                    unsigned long long nval = std::strtoull(v.c_str(), &end, 10);
                    if (errno == 0 && end && *end == '\0') {
                        content_length = static_cast<size_t>(nval);
                    }
                }
            }
            line_start = line_end + 2;
        }

        const size_t body_start = hdr_end + 4;
        if (out.size() >= body_start + content_length) return true;
    }
    return !out.empty();
}

static void split_path_query(const std::string& raw, std::string& path, std::string& query) {
    auto pos = raw.find('?');
    if (pos == std::string::npos) {
        path = raw;
        query.clear();
        return;
    }
    path = raw.substr(0, pos);
    query = raw.substr(pos + 1);
}

static int hex_to_int(char c) {
    if ('0' <= c && c <= '9') return c - '0';
    if ('a' <= c && c <= 'f') return c - 'a' + 10;
    if ('A' <= c && c <= 'F') return c - 'A' + 10;
    return -1;
}

static std::string url_decode(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (size_t i = 0; i < in.size(); ++i) {
        char ch = in[i];
        if (ch == '+') {
            out.push_back(' ');
            continue;
        }
        if (ch == '%' && i + 2 < in.size()) {
            int hi = hex_to_int(in[i + 1]);
            int lo = hex_to_int(in[i + 2]);
            if (hi >= 0 && lo >= 0) {
                out.push_back(static_cast<char>((hi << 4) | lo));
                i += 2;
                continue;
            }
        }
        out.push_back(ch);
    }
    return out;
}

static std::map<std::string, std::string> parse_query_map(const std::string& query) {
    std::map<std::string, std::string> out;
    size_t pos = 0;
    while (pos <= query.size()) {
        size_t amp = query.find('&', pos);
        std::string one = (amp == std::string::npos)
                              ? query.substr(pos)
                              : query.substr(pos, amp - pos);
        if (!one.empty()) {
            size_t eq = one.find('=');
            std::string k = (eq == std::string::npos) ? one : one.substr(0, eq);
            std::string v = (eq == std::string::npos) ? "" : one.substr(eq + 1);
            out[url_decode(k)] = url_decode(v);
        }
        if (amp == std::string::npos) break;
        pos = amp + 1;
    }
    return out;
}

static std::string map_get(const std::map<std::string, std::string>& m, const std::string& k) {
    auto it = m.find(k);
    if (it == m.end()) return "";
    return it->second;
}

static void parse_http_headers_and_body(const std::string& req,
                                        std::map<std::string, std::string>& headers,
                                        std::string& body) {
    headers.clear();
    body.clear();

    size_t hdr_end = req.find("\r\n\r\n");
    if (hdr_end == std::string::npos) return;
    body = req.substr(hdr_end + 4);

    std::istringstream iss(req.substr(0, hdr_end));
    std::string line;
    // request line
    std::getline(iss, line);

    while (std::getline(iss, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        auto colon = line.find(':');
        if (colon == std::string::npos) continue;
        std::string k = trim_copy(line.substr(0, colon));
        std::string v = trim_copy(line.substr(colon + 1));
        std::transform(k.begin(), k.end(), k.begin(), [](unsigned char c) {
            return static_cast<char>(std::tolower(c));
        });
        headers[k] = v;
    }
}

static bool parse_json_string_token(const std::string& s, size_t& pos, std::string& out) {
    out.clear();
    if (pos >= s.size() || s[pos] != '"') return false;
    ++pos;
    while (pos < s.size()) {
        char ch = s[pos++];
        if (ch == '"') return true;
        if (ch == '\\' && pos < s.size()) {
            char esc = s[pos++];
            switch (esc) {
                case '"':
                case '\\':
                case '/':
                    out.push_back(esc);
                    break;
                case 'b':
                    out.push_back('\b');
                    break;
                case 'f':
                    out.push_back('\f');
                    break;
                case 'n':
                    out.push_back('\n');
                    break;
                case 'r':
                    out.push_back('\r');
                    break;
                case 't':
                    out.push_back('\t');
                    break;
                default:
                    out.push_back(esc);
                    break;
            }
            continue;
        }
        out.push_back(ch);
    }
    return false;
}

static void parse_flat_json_object(const std::string& body, std::map<std::string, std::string>& out) {
    size_t pos = 0;
    auto skip_ws = [&](size_t& p) {
        while (p < body.size() && std::isspace(static_cast<unsigned char>(body[p])) != 0) ++p;
    };

    skip_ws(pos);
    if (pos >= body.size() || body[pos] != '{') return;
    ++pos;

    while (pos < body.size()) {
        skip_ws(pos);
        if (pos >= body.size()) break;
        if (body[pos] == '}') break;
        if (body[pos] == ',') {
            ++pos;
            continue;
        }

        std::string key;
        if (!parse_json_string_token(body, pos, key)) break;
        skip_ws(pos);
        if (pos >= body.size() || body[pos] != ':') break;
        ++pos;
        skip_ws(pos);
        if (pos >= body.size()) break;

        std::string value;
        if (body[pos] == '"') {
            if (!parse_json_string_token(body, pos, value)) break;
        } else {
            size_t start = pos;
            while (pos < body.size() && body[pos] != ',' && body[pos] != '}') ++pos;
            value = trim_copy(body.substr(start, pos - start));
        }
        if (!key.empty()) out[key] = value;
        skip_ws(pos);
        if (pos < body.size() && body[pos] == ',') ++pos;
    }
}

static void merge_body_params(const std::map<std::string, std::string>& headers,
                              const std::string& body,
                              std::map<std::string, std::string>& out) {
    if (body.empty()) return;
    std::string ctype = map_get(headers, "content-type");
    std::string lc = ctype;
    std::transform(lc.begin(), lc.end(), lc.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });

    std::map<std::string, std::string> parsed;
    if (lc.find("application/json") != std::string::npos) {
        parse_flat_json_object(body, parsed);
    } else if (lc.find("application/x-www-form-urlencoded") != std::string::npos ||
               lc.find("text/plain") != std::string::npos ||
               lc.empty()) {
        parsed = parse_query_map(body);
    }

    for (const auto& kv : parsed) {
        out[kv.first] = kv.second;
    }
}

static bool parse_uint64_param(const std::string& s, uint64_t& out) {
    if (s.empty()) return false;
    char* end = nullptr;
    errno = 0;
    unsigned long long v = std::strtoull(s.c_str(), &end, 10);
    if (errno != 0 || end == s.c_str() || (end && *end != '\0')) return false;
    out = static_cast<uint64_t>(v);
    return true;
}

static bool parse_int64_param(const std::string& s, int64_t& out) {
    if (s.empty()) return false;
    char* end = nullptr;
    errno = 0;
    long long v = std::strtoll(s.c_str(), &end, 10);
    if (errno != 0 || end == s.c_str() || (end && *end != '\0')) return false;
    out = static_cast<int64_t>(v);
    return true;
}

static bool parse_bool_param(const std::string& s, bool defv = false) {
    if (s.empty()) return defv;
    std::string t = s;
    std::transform(t.begin(), t.end(), t.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    if (t == "1" || t == "true" || t == "yes" || t == "on") return true;
    if (t == "0" || t == "false" || t == "no" || t == "off") return false;
    return defv;
}

static bool parse_request_arm_from_map(const std::map<std::string, std::string>& q,
                                       RequestArmRequest& arm,
                                       std::string& error_json) {
    arm.ns = !map_get(q, "namespace").empty() ? map_get(q, "namespace") : map_get(q, "ns");
    arm.pod = map_get(q, "pod");
    arm.container = map_get(q, "container").empty() ? "app" : map_get(q, "container");
    arm.source = !map_get(q, "source").empty() ? map_get(q, "source") : map_get(q, "source_id");
    arm.delay_ns = 0;
    arm.count = 1;

    uint64_t delay_ns = 0;
    std::string delay_ns_s = map_get(q, "delay_ns");
    if (delay_ns_s.empty()) delay_ns_s = map_get(q, "delayNs");
    if (!delay_ns_s.empty() && !parse_uint64_param(delay_ns_s, delay_ns)) {
        error_json = "{\"ok\":false,\"error\":\"invalid delay_ns\"}";
        return false;
    }
    arm.delay_ns = delay_ns;

    int64_t count = 1;
    std::string count_s = map_get(q, "count");
    if (!count_s.empty() && !parse_int64_param(count_s, count)) {
        error_json = "{\"ok\":false,\"error\":\"invalid count\"}";
        return false;
    }
    arm.count = count;

    if (arm.ns.empty() || arm.pod.empty()) {
        error_json = "{\"ok\":false,\"error\":\"namespace and pod are required\"}";
        return false;
    }

    error_json.clear();
    return true;
}

static bool parse_consume_policy_from_map(const std::map<std::string, std::string>& q,
                                          ConsumePolicyApplyRequest& policy,
                                          std::string& error_json) {
    policy.ns = !map_get(q, "namespace").empty() ? map_get(q, "namespace") : map_get(q, "ns");
    policy.pod = map_get(q, "pod");
    policy.container = map_get(q, "container").empty() ? "app" : map_get(q, "container");

    uint64_t raw_flags = 0;
    std::string raw_flags_s = map_get(q, "raw_flags");
    if (raw_flags_s.empty()) raw_flags_s = map_get(q, "rawFlags");
    if (!parse_uint64_param(raw_flags_s, raw_flags) || raw_flags == 0 || raw_flags > 0xFFFFFFFFULL) {
        error_json = "{\"ok\":false,\"error\":\"invalid raw_flags\"}";
        return false;
    }
    if (policy.ns.empty() || policy.pod.empty()) {
        error_json = "{\"ok\":false,\"error\":\"namespace and pod are required\"}";
        return false;
    }

    policy.raw_flags = static_cast<uint32_t>(raw_flags);
    error_json.clear();
    return true;
}

static void send_http_text(int fd, const std::string& status, const std::string& body) {
    std::string resp = "HTTP/1.1 " + status + "\r\n"
                       "Content-Type: text/plain\r\n"
                       "Connection: close\r\n"
                       "Content-Length: " + std::to_string(body.size()) + "\r\n\r\n" +
                       body;
    size_t sent = 0;
    while (sent < resp.size()) {
        ssize_t n = send(fd, resp.data() + sent, resp.size() - sent, 0);
        if (n <= 0) break;
        sent += static_cast<size_t>(n);
    }
}

static void send_http_json(int fd, const std::string& status, const std::string& body) {
    std::string resp = "HTTP/1.1 " + status + "\r\n"
                       "Content-Type: application/json\r\n"
                       "Connection: close\r\n"
                       "Content-Length: " + std::to_string(body.size()) + "\r\n\r\n" +
                       body;
    size_t sent = 0;
    while (sent < resp.size()) {
        ssize_t n = send(fd, resp.data() + sent, resp.size() - sent, 0);
        if (n <= 0) break;
        sent += static_cast<size_t>(n);
    }
}

static void clear_http_server_loop() {
    int port = load_clear_http_port();
    if (port <= 0) {
        std::cerr << "[WARN] invalid CLEAR_HTTP_PORT; /clear endpoint disabled\n";
        return;
    }

    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) {
        perror("socket(clear)");
        return;
    }
    int on = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = INADDR_ANY;
    if (bind(srv, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        perror("bind(clear)");
        close(srv);
        return;
    }
    if (listen(srv, 16) != 0) {
        perror("listen(clear)");
        close(srv);
        return;
    }
    std::cerr << "[INFO] control endpoint ready: GET/POST /clear,/rearm,/arm,/status,/syscall_profile on port "
              << port << "\n";

    while (g_running.load()) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(srv, &rfds);
        timeval tv{};
        tv.tv_sec = 0;
        tv.tv_usec = 200000;
        int rc = select(srv + 1, &rfds, nullptr, nullptr, &tv);
        if (rc <= 0) continue;

        sockaddr_in cli{};
        socklen_t cli_len = sizeof(cli);
        int cfd = accept(srv, reinterpret_cast<sockaddr*>(&cli), &cli_len);
        if (cfd < 0) continue;

        std::string req;
        (void)recv_http_request(cfd, req);

        std::string method, path;
        if (!parse_http_method_path(req, method, path)) {
            send_http_text(cfd, "400 Bad Request", "bad request\n");
            close(cfd);
            continue;
        }

        std::string clean_path, query;
        split_path_query(path, clean_path, query);
        auto q = parse_query_map(query);
        std::map<std::string, std::string> headers;
        std::string body_payload;
        parse_http_headers_and_body(req, headers, body_payload);
        merge_body_params(headers, body_payload, q);

        if (clean_path == "/clear") {
            uint64_t prev = g_accumulated_delay_ns.exchange(0, std::memory_order_acq_rel);
            bool clear_credits = parse_bool_param(map_get(q, "clear_credits"), false);
            if (request_credit_is_active()) {
                request_credit_clear(clear_credits);
            }
            std::string body = "cleared accumulated_delay_ns from " + std::to_string(prev) + " to 0\n";
            send_http_text(cfd, "200 OK", body);
            std::cerr << "[INFO] /clear request handled: accumulated_delay_ns "
                      << prev << " -> 0\n";
        } else if (clean_path == "/rearm") {
            if (request_credit_is_active()) {
                std::string body = request_credit_rearm_message() + "\n";
                send_http_text(cfd, "200 OK", body);
                close(cfd);
                continue;
            }

            bool was_armed = g_request_armed.exchange(true, std::memory_order_acq_rel);
            bool injected = false;
            uint64_t injected_ns = 0;

            // Exact/request-aware fast path:
            // if configured, inject fixed delay immediately on rearm so the same request can observe it.
            uint64_t immediate_ns = g_rearm_immediate_delay_ns.load(std::memory_order_acquire);
            if (immediate_ns > 0) {
                if (apply_delay_to_cgroups({}, immediate_ns)) {
                    injected = true;
                    injected_ns = immediate_ns;
                    g_request_armed.store(false, std::memory_order_release); // consumed for this request
                    uint64_t accumulated_delay_ns =
                        g_accumulated_delay_ns.fetch_add(immediate_ns, std::memory_order_acq_rel) + immediate_ns;
                    std::cerr << "[MCOZ] request_aware_rearm_injected"
                              << " delay_ns=" << immediate_ns
                              << " accumulated_delay_ns=" << accumulated_delay_ns
                              << " armed=0\n";
                } else {
                    std::cerr << "[WARN] /rearm immediate inject skipped: no victim pids\n";
                }
            }

            std::string body = "rearmed request-aware gate from " +
                               std::string(was_armed ? "1" : "0") + " to 1";
            if (injected) {
                body += " (immediate injected " + std::to_string(injected_ns) + "ns, armed->0)";
            }
            body += "\n";
            send_http_text(cfd, "200 OK", body);
            std::cerr << "[INFO] /rearm request handled: armed "
                      << (was_armed ? "1" : "0") << " -> "
                      << (g_request_armed.load(std::memory_order_acquire) ? "1" : "0")
                      << (injected ? " (with immediate inject)" : "")
                      << "\n";
        } else if (clean_path == "/arm") {
            if (!request_credit_is_active()) {
                send_http_json(cfd, "409 Conflict",
                               "{\"ok\":false,\"error\":\"request-credit mode is not active\"}");
                close(cfd);
                continue;
            }

            RequestArmRequest arm;
            std::string parse_error;
            if (!parse_request_arm_from_map(q, arm, parse_error)) {
                send_http_json(cfd, "400 Bad Request", parse_error);
                close(cfd);
                continue;
            }

            std::string body;
            bool ok = request_credit_arm(arm, body);
            send_http_json(cfd, ok ? "200 OK" : "500 Internal Server Error", body);
        } else if (clean_path == "/status") {
            if (request_credit_is_active()) {
                send_http_json(cfd, "200 OK", request_credit_status_json());
            } else {
                std::string body =
                    std::string("{\"ok\":true,\"mode\":\"legacy\",\"accumulated_delay_ns\":") +
                    std::to_string(g_accumulated_delay_ns.load(std::memory_order_acquire)) + "}";
                send_http_json(cfd, "200 OK", body);
            }
        } else if (clean_path == "/syscall_profile" || clean_path == "/syscall-profile") {
            if (!request_credit_is_active()) {
                send_http_json(cfd, "409 Conflict",
                               "{\"ok\":false,\"error\":\"request-credit mode is not active\"}");
                close(cfd);
                continue;
            }

            SyscallProfileRequest profile;
            profile.ns = !map_get(q, "namespace").empty() ? map_get(q, "namespace") : map_get(q, "ns");
            profile.pod = map_get(q, "pod");
            profile.container = map_get(q, "container").empty() ? "app" : map_get(q, "container");

            uint64_t duration_ms = 2000;
            std::string duration_s = map_get(q, "duration_ms");
            if (duration_s.empty()) duration_s = map_get(q, "durationMs");
            if (!duration_s.empty() && !parse_uint64_param(duration_s, duration_ms)) {
                send_http_json(cfd, "400 Bad Request",
                               "{\"ok\":false,\"error\":\"invalid duration_ms\"}");
                close(cfd);
                continue;
            }
            uint64_t top_k_raw = 12;
            std::string top_k_s = map_get(q, "top_k");
            if (top_k_s.empty()) top_k_s = map_get(q, "topK");
            if (!top_k_s.empty() && !parse_uint64_param(top_k_s, top_k_raw)) {
                send_http_json(cfd, "400 Bad Request",
                               "{\"ok\":false,\"error\":\"invalid top_k\"}");
                close(cfd);
                continue;
            }

            profile.duration_ms = duration_ms;
            profile.top_k = static_cast<uint32_t>(std::min<uint64_t>(top_k_raw, 64));
            if (profile.top_k == 0) profile.top_k = 1;
            profile.apply_policy =
                parse_bool_param(map_get(q, "apply_policy"),
                                 parse_bool_param(map_get(q, "applyPolicy"),
                                                  parse_bool_param(map_get(q, "apply"), false)));

            std::string body;
            bool ok = request_credit_profile_syscalls(profile, body);
            send_http_json(cfd, ok ? "200 OK" : "500 Internal Server Error", body);
        } else if (clean_path == "/consume_policy" || clean_path == "/consume-policy") {
            if (!request_credit_is_active()) {
                send_http_json(cfd, "409 Conflict",
                               "{\"ok\":false,\"error\":\"request-credit mode is not active\"}");
                close(cfd);
                continue;
            }

            ConsumePolicyApplyRequest policy;
            std::string parse_error;
            if (!parse_consume_policy_from_map(q, policy, parse_error)) {
                send_http_json(cfd, "400 Bad Request", parse_error);
                close(cfd);
                continue;
            }

            std::string body;
            bool ok = request_credit_apply_consume_policy(policy, body);
            send_http_json(cfd, ok ? "200 OK" : "500 Internal Server Error", body);
        } else {
            send_http_text(cfd, "404 Not Found", "not found\n");
        }
        close(cfd);
    }
    close(srv);
}

static void arm_udp_server_loop() {
    int port = load_arm_udp_port();
    if (port <= 0) {
        std::cerr << "[WARN] invalid ARM_UDP_PORT; UDP arm endpoint disabled\n";
        return;
    }

    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        perror("socket(arm-udp)");
        return;
    }
    int on = 1;
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = INADDR_ANY;
    if (bind(sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        perror("bind(arm-udp)");
        close(sock);
        return;
    }

    std::cerr << "[INFO] UDP arm endpoint ready on port " << port << "\n";

    while (g_running.load()) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(sock, &rfds);
        timeval tv{};
        tv.tv_sec = 0;
        tv.tv_usec = 200000;
        int rc = select(sock + 1, &rfds, nullptr, nullptr, &tv);
        if (rc <= 0) continue;

        char buf[2048];
        sockaddr_in cli{};
        socklen_t cli_len = sizeof(cli);
        ssize_t n = recvfrom(
            sock,
            buf,
            sizeof(buf) - 1,
            0,
            reinterpret_cast<sockaddr*>(&cli),
            &cli_len
        );
        if (n <= 0) continue;
        buf[n] = '\0';

        if (!request_credit_is_active()) continue;

        auto q = parse_query_map(std::string(buf, static_cast<size_t>(n)));
        RequestArmRequest arm;
        std::string parse_error;
        if (!parse_request_arm_from_map(q, arm, parse_error)) {
            std::cerr << "[WARN] UDP /arm parse failed: " << parse_error << "\n";
            continue;
        }

        std::string body;
        if (!request_credit_arm(arm, body)) {
            std::cerr << "[WARN] UDP /arm failed: " << body << "\n";
        }
    }

    close(sock);
}

static void ensure_clear_http_server_started() {
    bool expected = false;
    if (!g_clear_server_started.compare_exchange_strong(expected, true)) {
        return;
    }
    std::thread(clear_http_server_loop).detach();
    bool udp_expected = false;
    if (g_arm_udp_server_started.compare_exchange_strong(udp_expected, true)) {
        std::thread(arm_udp_server_loop).detach();
    }
}

void start_local_control_server() {
    ensure_clear_http_server_started();
}

bool mcoz_keep_running() {
    return g_running.load(std::memory_order_acquire);
}

/* ───────────── sampler entry ───────────── */
int perf_sampler_sync(int cg_fd,
                      std::chrono::milliseconds period,
                      double speedup,
                      const std::vector<cgroup>& others,
                      const std::string& /*mode*/)
{
    g_rearm_immediate_delay_ns.store(0, std::memory_order_release);
    std::cerr << "[INFO] Starting SCOZ perf sampler: period=" << period.count()
              << "ms speedup=" << speedup << "x\n";

    if (cg_fd < 0) {
        std::cerr << "[ERROR] invalid target cgroup fd; aborting sampler\n";
        return 1;
    }
    ensure_clear_http_server_started();

    auto cpu_cnt = sysconf(_SC_NPROCESSORS_ONLN);
    struct PerfCpu {
        int cpu;
        int fd;
        uint64_t prev;
        bool has_prev;
    };
    std::vector<PerfCpu> perf;
    perf.reserve(cpu_cnt);

    for (int cpu = 0; cpu < cpu_cnt; ++cpu) {
        perf_event_attr pe{}; pe.size = sizeof(pe);
        pe.type          = PERF_TYPE_SOFTWARE;
        pe.config        = PERF_COUNT_SW_CPU_CLOCK;
        pe.sample_period = 0;
        pe.disabled      = 1;
        pe.read_format   = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
        pe.exclude_idle  = 1;

        int fd = perf_event_open(&pe, cg_fd, cpu, -1, PERF_FLAG_PID_CGROUP | PERF_FLAG_FD_CLOEXEC);
        if (fd < 0) {
            pe.type = PERF_TYPE_SOFTWARE;
            pe.config = PERF_COUNT_HW_CPU_CYCLES;
            fd = perf_event_open(&pe, cg_fd, cpu, -1, PERF_FLAG_PID_CGROUP | PERF_FLAG_FD_CLOEXEC);
        }
        if (fd < 0) {
            if (!is_cgv2 && access("/sys/fs/cgroup/perf_event", F_OK) != 0) {
                std::cerr << "[ERROR] cgroup v1 perf_event controller not mounted at /sys/fs/cgroup/perf_event.\n"
                          << "        Try: mount -t cgroup -o perf_event cgroup /sys/fs/cgroup/perf_event\n";
            }
            continue;
        }
        ioctl(fd, PERF_EVENT_IOC_ENABLE, 0);
        ioctl(fd, PERF_EVENT_IOC_RESET, 0);
        perf.push_back(PerfCpu{cpu, fd, 0, false});
    }

    if (perf.empty()) {
        std::cerr << "[ERROR] no perf events opened; aborting sampler\n";
        return 1;
    }

    const uint64_t period_ns = static_cast<uint64_t>(period.count()) * 1000000ULL;
    const uint64_t threshold_ns = period_ns / 10;
    std::cerr << "[INFO] sampling period=" << period.count()
              << "ms threshold=" << threshold_ns << "ns\n";
    bool warned_no_victims = false;

    uint64_t window_total_delta = 0;
    uint64_t window_ticks = 0;
    uint64_t window_read_ok = 0;
    uint64_t window_read_fail = 0;
    auto window_start = std::chrono::steady_clock::now();

    while (g_running.load()) {
        std::this_thread::sleep_for(period);

        uint64_t total_delta = 0;
        uint64_t read_ok = 0;
        uint64_t read_fail = 0;
        for (auto& p : perf) {
            uint64_t buf[3]{};
            if (read(p.fd, buf, sizeof(buf)) != sizeof(buf)) {
                read_fail++;
                continue;
            }
            read_ok++;
            uint64_t val = buf[0];
            if (p.has_prev && val >= p.prev) {
                total_delta += (val - p.prev);
            }
            p.prev = val;
            p.has_prev = true;
        }

        window_total_delta += total_delta;
        window_ticks++;
        window_read_ok += read_ok;
        window_read_fail += read_fail;

        auto now = std::chrono::steady_clock::now();
        if (now - window_start >= std::chrono::seconds(1)) {
            auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(now - window_start).count();
            double cpu_ms = static_cast<double>(window_total_delta) / 1e6;
            std::cerr << "[DEBUG] perf 1s window: cpu_clock=" << cpu_ms
                      << "ms ticks=" << window_ticks
                      << " read_ok=" << window_read_ok
                      << " read_fail=" << window_read_fail
                      << " elapsed=" << elapsed_ms << "ms\n";
            window_total_delta = 0;
            window_ticks = 0;
            window_read_ok = 0;
            window_read_fail = 0;
            window_start = now;
        }

        if (total_delta) {
            uint64_t delay_ns = static_cast<uint64_t>(total_delta * speedup);
            if (delay_ns >= threshold_ns) {
                uint64_t accumulated_delay_ns =
                    g_accumulated_delay_ns.fetch_add(delay_ns, std::memory_order_acq_rel) + delay_ns;
                double clock_ms = static_cast<double>(total_delta) / 1e6;
                std::cerr << "[INFO] perf threshold hit: cpu_clock="
                          << clock_ms << "ms over period="
                          << period.count() << "ms (threshold="
                          << threshold_ns << "ns, delay=" << delay_ns
                          << "ns, accumulated=" << accumulated_delay_ns << "ns)\n";
                std::cerr << "[MCOZ] threshold_hit"
                          << " threshold_ns=" << threshold_ns
                          << " delay_ns=" << delay_ns
                          << " accumulated_delay_ns=" << accumulated_delay_ns
                          << " cpu_clock_ns=" << total_delta
                          << "\n";
                bool sent = send_delay_to_peers(delay_ns, true);
                if (!sent) {
                    if (!apply_delay_to_cgroups(others, delay_ns)) {
                        if (!warned_no_victims) {
                        std::cerr << "[WARN] no victim pids; skipping local delay\n";
                            warned_no_victims = true;
                        }
                    } else {
                        warned_no_victims = false;
                    }
                } else if (others.empty() && !warned_no_victims) {
                    std::cerr << "[WARN] no victim cgroups; delay will be ignored locally\n";
                    warned_no_victims = true;
                }
            }
        }
    }

    for (auto& p : perf) {
        ioctl(p.fd, PERF_EVENT_IOC_DISABLE, 0);
        close(p.fd);
    }
    cleanup();
    return 0;
}

int fixed_delay_injector(std::chrono::milliseconds period,
                         uint64_t delay_ns,
                         const std::vector<cgroup>& victims)
{
    (void)period;
    g_rearm_immediate_delay_ns.store(0, std::memory_order_release);
    if (delay_ns == 0) {
        std::cerr << "[WARN] fixed_delay_ns is 0; nothing to inject\n";
        return 0;
    }

    ensure_clear_http_server_started();

    std::cerr << "[INFO] fixed-delay mode (one-shot): delay_ns=" << delay_ns << "\n";

    if (!apply_delay_to_cgroups(victims, delay_ns)) {
        std::cerr << "[WARN] no victim pids; fixed delay skipped\n";
        cleanup();
        return 0;
    }

    uint64_t accumulated_delay_ns =
        g_accumulated_delay_ns.fetch_add(delay_ns, std::memory_order_acq_rel) + delay_ns;
    std::cerr << "[MCOZ] fixed_delay_once"
              << " delay_ns=" << delay_ns
              << " accumulated_delay_ns=" << accumulated_delay_ns
              << "\n";

    cleanup();
    return 0;
}

int perf_sampler_request_aware(int cg_fd,
                               std::chrono::milliseconds period,
                               double speedup,
                               uint64_t fixed_delay_ns,
                               const std::vector<cgroup>& others,
                               const std::string& mode)
{
    (void)mode;
    g_rearm_immediate_delay_ns.store(fixed_delay_ns, std::memory_order_release);
    if (fixed_delay_ns == 0) {
        std::cerr << "[ERROR] request-aware mode requires fixed_delay_ns > 0\n";
        return 1;
    }
    g_request_armed.store(true, std::memory_order_release);
    std::cerr << "[INFO] request-aware mode enabled: threshold from perf, one-shot fixed delay="
              << fixed_delay_ns << "ns per rearm\n";

    std::cerr << "[INFO] request-aware mode rearm endpoint: POST /rearm on CLEAR_HTTP_PORT\n";

    if (cg_fd < 0) {
        std::cerr << "[ERROR] invalid target cgroup fd; aborting request-aware sampler\n";
        return 1;
    }

    ensure_clear_http_server_started();

    auto cpu_cnt = sysconf(_SC_NPROCESSORS_ONLN);
    struct PerfCpu {
        int cpu;
        int fd;
        uint64_t prev;
        bool has_prev;
    };
    std::vector<PerfCpu> perf;
    perf.reserve(cpu_cnt);

    for (int cpu = 0; cpu < cpu_cnt; ++cpu) {
        perf_event_attr pe{}; pe.size = sizeof(pe);
        pe.type          = PERF_TYPE_SOFTWARE;
        pe.config        = PERF_COUNT_SW_CPU_CLOCK;
        pe.sample_period = 0;
        pe.disabled      = 1;
        pe.read_format   = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
        pe.exclude_idle  = 1;

        int fd = perf_event_open(&pe, cg_fd, cpu, -1, PERF_FLAG_PID_CGROUP | PERF_FLAG_FD_CLOEXEC);
        if (fd < 0) {
            pe.type = PERF_TYPE_SOFTWARE;
            pe.config = PERF_COUNT_HW_CPU_CYCLES;
            fd = perf_event_open(&pe, cg_fd, cpu, -1, PERF_FLAG_PID_CGROUP | PERF_FLAG_FD_CLOEXEC);
        }
        if (fd < 0) continue;
        ioctl(fd, PERF_EVENT_IOC_ENABLE, 0);
        ioctl(fd, PERF_EVENT_IOC_RESET, 0);
        perf.push_back(PerfCpu{cpu, fd, 0, false});
    }

    if (perf.empty()) {
        std::cerr << "[ERROR] no perf events opened; aborting request-aware sampler\n";
        return 1;
    }

    const uint64_t period_ns = static_cast<uint64_t>(period.count()) * 1000000ULL;
    const uint64_t threshold_ns = period_ns / 10;
    std::cerr << "[INFO] request-aware sampling period=" << period.count()
              << "ms threshold=" << threshold_ns << "ns\n";

    bool warned_no_victims = false;

    while (g_running.load()) {
        std::this_thread::sleep_for(period);

        uint64_t total_delta = 0;
        for (auto& p : perf) {
            uint64_t buf[3]{};
            if (read(p.fd, buf, sizeof(buf)) != sizeof(buf)) continue;
            uint64_t val = buf[0];
            if (p.has_prev && val >= p.prev) {
                total_delta += (val - p.prev);
            }
            p.prev = val;
            p.has_prev = true;
        }

        if (!total_delta) continue;

        uint64_t delay_ns = static_cast<uint64_t>(total_delta * speedup);
        if (delay_ns < threshold_ns) continue;

        bool expected = true;
        if (!g_request_armed.compare_exchange_strong(expected, false, std::memory_order_acq_rel)) {
            continue;
        }

        std::cerr << "[MCOZ] request_aware_threshold_hit"
                  << " threshold_ns=" << threshold_ns
                  << " sampled_delay_ns=" << delay_ns
                  << " injected_fixed_delay_ns=" << fixed_delay_ns
                  << " cpu_clock_ns=" << total_delta
                  << "\n";

        bool sent = send_delay_to_peers(fixed_delay_ns, true);
        if (!sent) {
            if (!apply_delay_to_cgroups(others, fixed_delay_ns)) {
                if (!warned_no_victims) {
                    std::cerr << "[WARN] no victim pids; request-aware fixed delay skipped\n";
                    warned_no_victims = true;
                }
                continue;
            }
            warned_no_victims = false;
        } else if (others.empty() && !warned_no_victims) {
            std::cerr << "[WARN] no victim cgroups; request-aware delay may be ignored locally\n";
            warned_no_victims = true;
        }

        uint64_t accumulated_delay_ns =
            g_accumulated_delay_ns.fetch_add(fixed_delay_ns, std::memory_order_acq_rel) + fixed_delay_ns;
        std::cerr << "[MCOZ] request_aware_injected"
                  << " delay_ns=" << fixed_delay_ns
                  << " accumulated_delay_ns=" << accumulated_delay_ns
                  << " armed=0\n";
    }

    for (auto& p : perf) {
        ioctl(p.fd, PERF_EVENT_IOC_DISABLE, 0);
        close(p.fd);
    }
    cleanup();
    return 0;
}

/* ───────────── cleanup & sigint ───────────── */
void cleanup(){
    std::cerr << "[INFO] cleanup\n";
    for(auto& v:g_victims) close(v.fd);
    for (auto& per_cpu : g_protect_perf_fds) {
        for (int fd : per_cpu) if (fd >= 0) close(fd);
    }
    g_protect_perf_fds.clear();
    g_protect_prev_vals.clear();
    for (int fd : g_protect_cg_fds) if (fd >= 0) close(fd);
    g_protect_cg_fds.clear();
    }

void sigint_handler(int){ g_running=false; }

/* ───────────── alternate consumer‑only mode ───────────── */
int delay_consumer_only(const std::vector<cgroup>& victims)
{
    std::cerr << "[INFO] consumer-only mode (UDP delay receiver)\n";
    if (victims.empty()) {
        std::cerr << "[WARN] consumer-only mode: no victim cgroups; delaying nothing\n";
    }

    DelayNetConfig cfg = load_delay_net_config();
    if (cfg.port <= 0) {
        std::cerr << "[WARN] invalid DELAY_PORT; receiver disabled\n";
        return 0;
    }

    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        perror("socket");
        return 1;
    }
    int on = 1;
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(cfg.port);
    addr.sin_addr.s_addr = INADDR_ANY;
    if (bind(sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        perror("bind");
        close(sock);
        return 1;
    }
    std::cerr << "[INFO] UDP delay receiver listening on port " << cfg.port << "\n";

    bool warned_no_victims = victims.empty();
    while (g_running.load()) {
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(sock, &fds);
        timeval tv{};
        tv.tv_sec = 0;
        tv.tv_usec = 200000;
        int rc = select(sock + 1, &fds, nullptr, nullptr, &tv);
        if (rc <= 0) continue;
        uint64_t payload = 0;
        sockaddr_in src{};
        socklen_t slen = sizeof(src);
        ssize_t n = recvfrom(sock, &payload, sizeof(payload), 0,
                             reinterpret_cast<sockaddr*>(&src), &slen);
        if (n != (ssize_t)sizeof(payload)) continue;
        uint64_t delay_ns = be64toh(payload);
        if (delay_ns == 0) continue;
        char ipbuf[INET_ADDRSTRLEN]{};
        const char* ip = inet_ntop(AF_INET, &src.sin_addr, ipbuf, sizeof(ipbuf));
        std::cerr << "[INFO] delay signal received: ns=" << delay_ns
                  << " from=" << (ip ? ip : "?")
                  << ":" << ntohs(src.sin_port) << "\n";
        if (!apply_delay_to_cgroups(victims, delay_ns)) {
            if (!warned_no_victims) {
                std::cerr << "[WARN] no victim pids; skipping injected delay\n";
                warned_no_victims = true;
            }
        } else {
            warned_no_victims = false;
        }
    }
    close(sock);
    cleanup();
    return 0;
}
