#include "perf.h"
#include <unistd.h>
#include <getopt.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <filesystem>
#include <iostream>
#include <fstream>
#include <vector>
#include <chrono>
#include <thread>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <csignal>   // signal(), SIGINT  ← C++ 헤더
#include <cstdlib>   // atexit()
#include <algorithm>   // std::find, std::all_of ...
#include <sstream>     // std::istringstream  (discover_other_pods에서 사용)

/* 0. kubeconfig 경로 확보*/
const char* kc = getenv("KUBECONFIG");      // 환경변수 읽기
std::string kc_arg = kc ? (" --kubeconfig " + std::string(kc)) : "";

static bool is_system_ns(const std::string& ns)
{
    /* 필요 시 목록 추가 */
    static const std::vector<std::string> sys = {
        "kube-system", "kube-public", "kube-node-lease"
    };
    return std::find(sys.begin(), sys.end(), ns) != sys.end();
}

static uint64_t get_cgroup_id(const std::string& path) {
    struct stat st;
    if(stat(path.c_str(), &st) != 0) return 0;
    return st.st_ino;
}

static std::string trim(const std::string& s) {
    if (s.empty()) return s;
    size_t end = s.find_last_not_of(" \n\r\t");
    size_t start = s.find_first_not_of(" \n\r\t");
    if (start == std::string::npos) return "";
    return s.substr(start, end - start + 1);
}

static std::string wait_container_id(const std::string& ns, const std::string& pod) {
    // Block until the pod exists and has a containerID
    // Uses polling for robustness; avoids relying on kubectl streaming semantics.
    std::string cid;
    while (true) {
        std::string cmd = "kubectl get pod " + pod + " -n " + ns + kc_arg +
                          " -o jsonpath={.status.containerStatuses[0].containerID}";
        FILE* fp = popen(cmd.c_str(), "r");
        if (fp) {
            char buf[256];
            if (fgets(buf, sizeof(buf), fp)) cid = buf; else cid.clear();
            pclose(fp);
            cid = trim(cid);
            if (!cid.empty() && cid != "null" && cid != "<no value>") {
                return cid;
            }
        }
        std::cerr << "[wait] Target pod " << ns << "/" << pod << " not ready; waiting...\n";
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
}

static cgroup resolve_target_cgroup(const std::string& target_pod) {
    size_t slash = target_pod.find('/');
    if(slash == std::string::npos) {
        throw std::runtime_error("invalid pod format namespace/pod");
    }
    std::string ns = target_pod.substr(0, slash);
    std::string pod = target_pod.substr(slash+1);

    // Wait for the containerID to become available
    std::string cid = wait_container_id(ns, pod);
    const std::string prefix = "containerd://";
    if(cid.rfind(prefix,0)==0) cid = cid.substr(prefix.size());

    // 다양한 cgroup 레이아웃 지원: v2(순수)/unified, v1(perf_event), systemd/cgroupfs
    std::vector<std::string> bases = {
        "/sys/fs/cgroup",
        "/sys/fs/cgroup/unified",
        "/sys/fs/cgroup/perf_event"
    };
    std::vector<std::string> start_dirs;
    for (const auto& b : bases) {
        if (access(b.c_str(), F_OK) == 0) {
            start_dirs.push_back(b);
            // Common subtrees used by kubelet
            std::vector<std::string> subs = {"/kubepods.slice", "/kubepods", "/system.slice"};
            for (const auto& s : subs) {
                std::string p = b + s;
                if (access(p.c_str(), F_OK) == 0) start_dirs.push_back(p);
            }
        }
    }
    // Search for either a systemd scope name or any directory containing the CID
    const std::string scope_needle = "cri-containerd-" + cid + ".scope";
    std::string found;
    for (const auto& root : start_dirs) {
        try {
            for (const auto& dir : std::filesystem::recursive_directory_iterator(
                     root, std::filesystem::directory_options::skip_permission_denied)) {
                if (!dir.is_directory()) continue;
                const auto fname = dir.path().filename().string();
                if (fname == scope_needle || fname.find(cid) != std::string::npos) {
                    found = dir.path().string();
                    break;
                }
            }
        } catch (const std::exception&) {
            // ignore traversal errors and try next root
        }
        if (!found.empty()) break;
    }
    if(found.empty()) {
        throw std::runtime_error("cgroup path not found for cid="+cid);
    }
    cgroup cg{found, get_cgroup_id(found)};
    std::cout << "found cgroup id: " << cg.id << std::endl;
    return cg;
}

static void inject_delay(const std::vector<cgroup>& others, uint64_t usec, const std::string& mode) {
    for(const auto& cg : others) {
        if(mode == "freezer") {
            std::ofstream(cg.path + "/cgroup.freeze") << '1';
            std::this_thread::sleep_for(std::chrono::microseconds(usec));
            std::ofstream(cg.path + "/cgroup.freeze") << '0';
        } else if(mode == "cpu-weight") {
            std::ofstream(cg.path + "/cpu.weight") << "1";
            std::this_thread::sleep_for(std::chrono::microseconds(usec));
            std::ofstream(cg.path + "/cpu.weight") << "100";
        }
    }
}

// select victims
static std::vector<cgroup>
discover_other_pods(const cgroup& tgt, const std::string& exclude /* 예: ns/pod */)
{
    std::vector<cgroup> out;
    std::cout << "In >> discover_other_pods\n";

    // DEBUG : 오잉 이거 empty다
    std::cout << "target : " << exclude << std::endl;

    /* 1. kubectl 로 전체 Pod 목록 추출 */
    std::string cmd =
        "kubectl get pods --all-namespaces" +
        kc_arg +
        " -o "
        "jsonpath='{range .items[*]}{.metadata.namespace} "
        "{.metadata.name} "
        "{.status.containerStatuses[0].containerID}{\"\\n\"}{end}'";
    FILE* fp = popen(cmd.c_str(), "r");   // ← .c_str() 중요
    if (!fp) { perror("kubectl"); return out; }

    /* 2. cgroup 탐색 시작 디렉터리 집합 구성 */
    std::vector<std::string> bases = {
        "/sys/fs/cgroup",
        "/sys/fs/cgroup/unified",
        "/sys/fs/cgroup/perf_event"
    };
    std::vector<std::string> start_dirs;
    for (const auto& b : bases) {
        if (access(b.c_str(), F_OK) == 0) {
            start_dirs.push_back(b);
            std::vector<std::string> subs = {"/kubepods.slice", "/kubepods", "/system.slice"};
            for (const auto& s : subs) {
                std::string p = b + s;
                if (access(p.c_str(), F_OK) == 0) start_dirs.push_back(p);
            }
        }
    }

    char line[512];
    while (fgets(line, sizeof(line), fp)) {
        std::string ns, pod, cid;
        std::istringstream iss(line);
        iss >> ns >> pod >> cid;
        if (!cid.empty()) {
            size_t pos = cid.find_last_not_of(" \n\r\t");
            if (pos != std::string::npos) cid.erase(pos + 1);
            else cid.clear();
        }

        /* 2-1. system NS·타깃·exclude 필터 */
        if (is_system_ns(ns))             continue;
        if (!exclude.empty() && ns + "/" + pod == exclude) {
            // std::cout << "[SKIP] " << ns << "/" << pod
            //           << ", Target excluded" << std::endl;
            continue;
        }
        /* 2-2. containerd:// prefix 제거 */
        const std::string pref = "containerd://";
        if (cid.rfind(pref, 0) == 0) cid = cid.substr(pref.size());

        /* 2-3. cgroup 경로 탐색 (cri-containerd-<cid>.scope) */
        const std::string needle = "cri-containerd-" + cid + ".scope";
        std::string found;
        for (const auto& root : start_dirs) {
            try {
                for (const auto& dir : std::filesystem::recursive_directory_iterator(
                         root, std::filesystem::directory_options::skip_permission_denied))
                {
                    if (!dir.is_directory()) continue;
                    const auto fname = dir.path().filename().string();
                    if (fname == needle || fname.find(cid) != std::string::npos) {
                        found = dir.path().string();
                        break;
                    }
                }
            } catch (const std::exception&) {
                // skip errors and continue
            }
            if (!found.empty()) break;
        }
        if (found.empty() || found == tgt.path) {
            // std::cout << "[SKIP] Pod=" << ns << "/" << pod
            //     << ": No Cgroup" << std::endl;
            continue; // 타깃 제외
        }

        // DEBUG: VICTIM으로 선정된 pod 정보
        // std::cout << "[VICTIM] Pod=" << ns << "/" << pod
        //           << ", cgroup path=" << found << std::endl;

        out.push_back({found, get_cgroup_id(found)});
    }
    pclose(fp);

    // std::cout << "discover_other_pods: " << out.size() << " victims\n";
    // std::cout << "Out >> discover_other_pods\n";
    return out;
}

static std::string get_pod_node(const std::string& ns, const std::string& pod) {
    std::string cmd = "kubectl get pod " + pod + " -n " + ns + kc_arg +
                      " -o jsonpath={.spec.nodeName}";
    FILE* fp = popen(cmd.c_str(), "r");
    if (!fp) return "";
    char buf[256];
    std::string node;
    if (fgets(buf, sizeof(buf), fp)) node = buf;
    pclose(fp);
    // trim
    size_t end = node.find_last_not_of(" \n\r\t");
    if (end != std::string::npos) node.erase(end+1);
    return node;
}

int main(int argc, char** argv) {
    printf("In >> main\n");
    std::cerr << "`DBG] my pid=" << getpid() << " tid=" << gettid() << '\n';

    const char* target_pod = nullptr;
    double speedup = 0.1;
    int period_ms = 5;

    signal(SIGINT, sigint_handler);  // Ctrl-C 누르면 poll 루프 탈출
    atexit(cleanup);                 // 어떤 경로든 종료 시 해동 보장   

    static struct option opts[] = {
        {"target-pod", required_argument, 0, 't'},
        {"speedup", required_argument, 0, 's'},
        {0,0,0,0}
    };

    int opt;
    while((opt = getopt_long(argc, argv, "t:p:s:", opts, nullptr)) != -1) {
        switch(opt) {
        case 't': target_pod = optarg; break;
        case 's': speedup = atof(optarg); break;
        }
    }

    if(!target_pod) {
        std::cerr << "need --target-pod" << std::endl;
        return 1;
    }

    try {
        // start CRD integration threads
        start_crd_threads();
        // Normalize target: allow POD only by prefixing env NAMESPACE (or default)
        std::string tp(target_pod);
        if (tp.find('/') == std::string::npos) {
            const char* nsEnv = std::getenv("NAMESPACE");
            std::string ns = (nsEnv && *nsEnv) ? nsEnv : std::string("default");
            tp = ns + "/" + tp;
        }
        // node locality check
        size_t slash = tp.find('/');
        std::string ns = tp.substr(0, slash);
        std::string pod = tp.substr(slash+1);
        const char* my_node = std::getenv("NODE_NAME");
        std::string target_node = get_pod_node(ns, pod);
        std::cout << "local node=" << (my_node?my_node:"") << " target node=" << target_node << std::endl;

        if (!my_node || target_node.empty() || target_node == my_node) {
            // Same node (or unknown): do full perf sampling
            std::cerr << "[INFO] target on same node; running perf" << std::endl;
            cgroup tgt = resolve_target_cgroup(tp);
            std::cout << "target cgroup path : " << tgt.path << std::endl;
            int cg_fd = open(tgt.path.c_str(), O_DIRECTORY);
            if(cg_fd < 0) { perror("open cgroup"); return 1; }
            std::cout << "cg_fd : " << cg_fd << std::endl;
            auto others = discover_other_pods(tgt, tp);
            perf_sampler_sync(cg_fd, std::chrono::milliseconds(period_ms), speedup, others, "freezer");
        } else {
            // Different node: don't do perf; only consume CRD-driven delay
            std::cerr << "[INFO] target on different node; running consumer-only" << std::endl;
            delay_consumer_only(speedup);
        }
        // stop CRD threads after work is done
        stop_crd_threads();
        
    } catch(const std::exception& e) {
        std::cerr << "error: " << e.what() << std::endl;
        return 1;
    }
    return 0;
}
