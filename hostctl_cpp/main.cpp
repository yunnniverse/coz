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
#include <cstring>
#include <csignal>   // signal(), SIGINT  ← C++ 헤더
#include <cstdlib>   // atexit()
#include <algorithm>   // std::find, std::all_of ...
#include <sstream>     // std::istringstream  (discover_other_pods에서 사용)
#include <unistd.h> // access
#include <cctype>
#include <cerrno>
#include <set>
#include <map>

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

static std::string strip_container_prefix(const std::string& cid) {
    auto pos = cid.find("://");
    if (pos == std::string::npos) return cid;
    return cid.substr(pos + 3);
}

static bool cgroup_tasks_has_pid(const std::string& cg_path) {
    std::ifstream in(cg_path + "/tasks");
    if (!in.good()) return false;
    long v = 0;
    return static_cast<bool>(in >> v) && v > 0;
}

static std::string find_cgroup_by_tokens(const std::vector<std::string>& roots,
                                         const std::vector<std::string>& tokens) {
    std::vector<std::string> strong_tokens;
    strong_tokens.reserve(tokens.size());
    for (const auto& tok : tokens) {
        if (tok.find("cri-containerd-") != std::string::npos ||
            tok.find(".scope") != std::string::npos) {
            strong_tokens.push_back(tok);
        }
    }

    auto scan = [&](const std::vector<std::string>& scan_tokens, bool require_tasks_non_empty) -> std::string {
        std::string fallback;
        for (const auto& root : roots) {
            try {
                for (const auto& dir : std::filesystem::recursive_directory_iterator(
                         root, std::filesystem::directory_options::skip_permission_denied)) {
                    if (!dir.is_directory()) continue;
                    const auto fname = dir.path().filename().string();
                    const auto full = dir.path().string();
                    for (const auto& tok : scan_tokens) {
                        if (tok.empty()) continue;
                        if (fname.find(tok) == std::string::npos &&
                            full.find(tok) == std::string::npos) {
                            continue;
                        }
                        if (!require_tasks_non_empty || cgroup_tasks_has_pid(full)) {
                            return full;
                        }
                        if (fallback.empty()) fallback = full;
                    }
                }
            } catch (const std::exception&) {
                // skip errors and continue
            }
        }
        return fallback;
    };

    if (!strong_tokens.empty()) {
        std::string hit = scan(strong_tokens, true);
        if (!hit.empty()) return hit;
        hit = scan(strong_tokens, false);
        if (!hit.empty()) return hit;
    }

    std::string hit = scan(tokens, true);
    if (!hit.empty()) return hit;
    return scan(tokens, false);
}

static std::string wait_container_id(const std::string& ns, const std::string& pod) {
    // Block until the pod exists and has a containerID
    // Uses polling for robustness; avoids relying on kubectl streaming semantics.
    std::string cid;
    while (true) {
        std::string cmd = "kubectl get pod " + pod + " -n " + ns + kc_arg +
                          " -o jsonpath={.status.containerStatuses[0].containerID} 2>/dev/null";
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
        // quiet wait loop to reduce noise
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

// // select victims
// static std::vector<cgroup>
// discover_other_pods(const cgroup& tgt, const std::string& exclude /* 예: ns/pod */)
// {
//     std::vector<cgroup> out;
//     static std::set<std::string> logged;

//     /* 1. kubectl 로 전체 Pod 목록 추출 */
//     std::string cmd =
//         "kubectl get pods --all-namespaces" +
//         kc_arg +
//         " -o "
//         "jsonpath=\"{range .items[*]}{.metadata.namespace}|"
//         "{.metadata.name}|"
//         "{.metadata.uid}|"
//         "{.metadata.annotations.mcoz-test}|"
//         "{.spec.nodeName}|"
//         "{.status.containerStatuses[0].containerID}{\"\\n\"}{end}\""
//         " 2>/dev/null";
//     FILE* fp = popen(cmd.c_str(), "r");   // ← .c_str() 중요
//     if (!fp) { perror("kubectl"); return out; }

//     /* 2. cgroup 탐색 시작 디렉터리 집합 구성 */
//     std::vector<std::string> bases = {
//         "/sys/fs/cgroup",
//         "/sys/fs/cgroup/unified",
//         "/sys/fs/cgroup/perf_event"
//     };
//     std::vector<std::string> start_dirs;
//     for (const auto& b : bases) {
//         if (access(b.c_str(), F_OK) == 0) {
//             start_dirs.push_back(b);
//             std::vector<std::string> subs = {"/kubepods.slice", "/kubepods", "/system.slice"};
//             for (const auto& s : subs) {
//                 std::string p = b + s;
//                 if (access(p.c_str(), F_OK) == 0) start_dirs.push_back(p);
//             }
//         }
//     }

//     char line[512];
//     while (fgets(line, sizeof(line), fp)) {
//         std::string ns, pod, uid, ann, node, cid;
//         std::istringstream iss(line);
//         if (!std::getline(iss, ns, '|')) continue;
//         if (!std::getline(iss, pod, '|')) continue;
//         if (!std::getline(iss, uid, '|')) uid.clear();
//         if (!std::getline(iss, ann, '|')) ann.clear();
//         if (!std::getline(iss, node, '|')) node.clear();
//         if (!std::getline(iss, cid, '|')) cid.clear();
//         ns = trim(ns);
//         pod = trim(pod);
//         uid = trim(uid);
//         ann = trim(ann);
//         node = trim(node);
//         cid = trim(cid);
//         if (ns.empty() || pod.empty()) continue;

//         /* 2-1. system NS·타깃·exclude 필터 */
//         if (is_system_ns(ns))             continue;
//         if (!exclude.empty() && ns + "/" + pod == exclude) {
//             // std::cout << "[SKIP] " << ns << "/" << pod
//             //           << ", Target excluded" << std::endl;
//             continue;
//         }
//         const char* my_node = std::getenv("NODE_NAME");
//         if (my_node && *my_node && !node.empty() && node != my_node) {
//             continue;
//         }
//         if (ann.empty() || ann == "<no value>" || ann == "null") {
//             continue;
//         }
//         /* 2-2. runtime prefix 제거 */
//         if (!cid.empty()) {
//             cid = strip_container_prefix(cid);
//         }
//         if (cid.empty() && uid.empty()) continue;

//         /* 2-3. cgroup 경로 탐색 (cri-containerd-<cid>.scope) */
//         std::vector<std::string> tokens;
//         if (!cid.empty()) {
//             tokens.push_back("cri-containerd-" + cid + ".scope");
//             tokens.push_back(cid);
//         }
//         if (!uid.empty()) {
//             std::string uid_under = uid;
//             std::replace(uid_under.begin(), uid_under.end(), '-', '_');
//             tokens.push_back("pod" + uid_under);
//             tokens.push_back("pod" + uid);
//         }
//         std::string found = find_cgroup_by_tokens(start_dirs, tokens);
//         if (found.empty() || found == tgt.path) {
//             // std::cout << "[SKIP] Pod=" << ns << "/" << pod
//             //     << ": No Cgroup" << std::endl;
//             continue; // 타깃 제외
//         }

//         std::string key = ns + "/" + pod;
//         if (logged.insert(key).second) {
//             std::cerr << "[INFO] victim pod (mcoz-test): " << key << "\n";
//         }

//         out.push_back({found, get_cgroup_id(found)});
//     }
//     pclose(fp);

//     return out;
// }

static std::vector<cgroup>
discover_other_pods(const cgroup& tgt, const std::string& exclude)
{
    std::vector<cgroup> out;
    static std::set<std::string> logged;

    // 1. [LOG] 시작 알림
    std::cerr << "--- [Discover] Start scanning for pods ---" << std::endl;

    // 내 노드 이름 가져오기
    const char* my_node_env = std::getenv("NODE_NAME");
    std::string my_node_str = (my_node_env) ? my_node_env : "";
    
    if (my_node_str.empty()) {
        std::cerr << "[WARN] NODE_NAME env is empty! Filtering might fail." << std::endl;
    } else {
        std::cerr << "[INFO] My Node Name: " << my_node_str << std::endl;
    }

    // 2. kubectl 명령어 구성 (stderr 살리기: 2>/dev/null 제거)
    std::string cmd =
        "kubectl get pods --all-namespaces" +
        kc_arg +
        " -o "
        "jsonpath=\"{range .items[*]}"
        "{.metadata.namespace}|"
        "{.metadata.name}|"
        "{.metadata.uid}|" // UID 추가
        "{.metadata.annotations.mcoz-test}|"
        "{.spec.nodeName}|"
        "{.status.containerStatuses[0].containerID}"
        "{\\\"\\n\\\"}{end}\"" // 개행 문자 처리
        " 2>&1"; // <--- 여기가 핵심! 에러를 stdout으로 리다이렉션

    // [디버깅] 실제로 실행되는 명령어가 뭔지 로그에 찍어보기 (복사해서 쉘에서 돌려볼 수 있음)
    std::cerr << "[DEBUG] Executing CMD: " << cmd << std::endl;

    FILE* fp = popen(cmd.c_str(), "r");
    if (!fp) { perror("popen kubectl failed"); return out; }

    /* 2. cgroup 탐색 시작 디렉터리 집합 구성 */
    // ... (이 부분은 기존 코드 유지) ...
    std::vector<std::string> bases = {
        "/sys/fs/cgroup", "/sys/fs/cgroup/unified", "/sys/fs/cgroup/perf_event"
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

    char line[4096]; // 버퍼를 넉넉하게 (에러 메시지가 길 수 있음)
    int scan_count = 0;

    while (fgets(line, sizeof(line), fp)) {
        scan_count++;
        std::string raw_line(line);
        
        // [디버깅] kubectl이 뱉는 모든 줄을 출력 (에러 메시지 확인용)
        // 정상 작동 확인되면 주석 처리 하세요.
        // std::cerr << "[RAW OUTPUT] " << raw_line; 

        // 만약 에러 메시지라면 파싱하지 말고 건너뛰거나 로그 출력
        if (raw_line.find("Error") != std::string::npos || raw_line.find("forbidden") != std::string::npos) {
            std::cerr << "[KUBECTL ERROR] " << raw_line;
            continue;
        }

        std::string ns, pod, uid, ann, node, cid;
        std::istringstream iss(line);

        // [컴파일 에러 수정] 리턴 타입을 std::istream& 로 명시
        auto get_token = [&](std::string& s) -> std::istream& { 
            return std::getline(iss, s, '|'); 
        };

        if (!get_token(ns)) continue;
        if (!get_token(pod)) continue;
        if (!get_token(uid)) uid = "";
        if (!get_token(ann)) ann = "";
        if (!get_token(node)) node = "";
        if (!get_token(cid)) cid = "";

        ns = trim(ns); pod = trim(pod); uid = trim(uid);
        ann = trim(ann); node = trim(node); cid = trim(cid);

        if (ns.empty() || pod.empty()) continue;

        // [DEBUG] 원시 데이터 확인 (너무 많으면 주석 처리)
        // std::cerr << "[RAW] " << ns << "/" << pod << " | Node:" << node << " | Ann:" << ann << "\n";

        // 2-1. 시스템 네임스페이스 및 Exclude 필터
        if (is_system_ns(ns)) continue;
        if (!exclude.empty() && ns + "/" + pod == exclude) continue;

        // 2-2. [Check] 노드 일치 여부 확인 (가장 중요한 실패 지점)
        if (!my_node_str.empty() && !node.empty()) {
            if (node != my_node_str) {
                // 노드가 다르면 조용히 스킵 (로그 너무 많아짐)
                continue; 
            }
        }

        // 여기서부터는 내 노드에 있는 Pod임.
        // 2-3. [Check] Annotation 확인
        if (ann != "true") {
            // std::cerr << "[SKIP] " << ns << "/" << pod << " (Annotation mismatch: '" << ann << "')\n";
            continue;
        }

        std::cerr << "[MATCH] Found Target Candidate: " << ns << "/" << pod << "\n";

        // 2-4. Container ID 정제
        if (!cid.empty()) {
            // containerd:// 제거 등
            std::string prefix = "containerd://";
            size_t pos = cid.find(prefix);
            if (pos != std::string::npos) cid = cid.substr(pos + prefix.length());
            
            prefix = "cri-o://"; // 혹시 모를 crio 대비
            pos = cid.find(prefix);
            if (pos != std::string::npos) cid = cid.substr(pos + prefix.length());
        }

        if (cid.empty() && uid.empty()) {
             std::cerr << "[ERR] " << ns << "/" << pod << " has no CID and no UID. Skipping.\n";
             continue;
        }

        // 2-5. Cgroup 토큰 생성
        std::vector<std::string> tokens;
        if (!cid.empty()) {
            tokens.push_back(cid); // CID 자체
            tokens.push_back("cri-containerd-" + cid + ".scope"); // Systemd scope 이름
        }
        if (!uid.empty()) {
            std::string uid_under = uid;
            std::replace(uid_under.begin(), uid_under.end(), '-', '_');
            tokens.push_back("pod" + uid_under); // Systemd slice 이름 (pod<UID>.slice)
            tokens.push_back(uid); // UID 자체
        }

        // 2-6. Cgroup 탐색 실행
        // (find_cgroup_by_tokens 함수가 정확하다는 가정하에 호출)
        std::string found = find_cgroup_by_tokens(start_dirs, tokens);

        if (found.empty()) {
            std::cerr << "   -> [FAIL] Cgroup NOT found for " << ns << "/" << pod << "\n";
            // 경로 권한 문제인지, 경로 패턴이 다른지 확인 필요
            continue; 
        } 
        
        if (found == tgt.path) {
            std::cerr << "   -> [SKIP] Self detection.\n";
            continue;
        }

        std::string key = ns + "/" + pod;
        if (logged.insert(key).second) {
            std::cerr << "[SUCCESS] Registered victim pod: " << key << " @ " << found << "\n";
        }
        
        out.push_back({found, get_cgroup_id(found)});
    }
    
    if (scan_count == 0) {
        std::cerr << "[WARN] kubectl output was empty. Check RBAC permissions.\n";
    }
    
    pclose(fp);
    std::cerr << "--- [Discover] End ---\n";
    return out;
}

static std::vector<cgroup> wait_for_victims(const cgroup& tgt, const std::string& exclude) {
    while (true) {
        auto others = discover_other_pods(tgt, exclude);
        if (!others.empty()) {
            size_t pid_cnt = prewarm_victims(others);
            if (pid_cnt > 0) {
                std::cerr << "[INFO] victims ready: pods=" << others.size()
                          << " pids=" << pid_cnt << "\n";
                return others;
            }
            std::cerr << "[INFO] victims found but no pids yet; waiting...\n";
        } else {
            std::cerr << "[INFO] no annotated victims yet; waiting...\n";
        }
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
}

static std::vector<pid_t> parse_victim_pid_csv(const std::string& csv) {
    std::vector<pid_t> out;
    std::string token;
    std::istringstream iss(csv);
    while (std::getline(iss, token, ',')) {
        token = trim(token);
        if (token.empty()) continue;
        char* end = nullptr;
        long v = std::strtol(token.c_str(), &end, 10);
        if (end == token.c_str() || (end && *end != '\0') || v <= 0) continue;
        out.push_back(static_cast<pid_t>(v));
    }
    if (!out.empty()) {
        std::sort(out.begin(), out.end());
        out.erase(std::unique(out.begin(), out.end()), out.end());
    }
    return out;
}

static bool parse_uint64(const std::string& s, uint64_t& out) {
    if (s.empty()) return false;
    if (!std::all_of(s.begin(), s.end(), [](unsigned char ch) { return std::isdigit(ch) != 0; })) {
        return false;
    }
    char* end = nullptr;
    errno = 0;
    unsigned long long v = std::strtoull(s.c_str(), &end, 10);
    if (errno != 0 || end == s.c_str() || (end && *end != '\0')) return false;
    out = static_cast<uint64_t>(v);
    return true;
}

static std::string shell_quote(const std::string& s) {
    std::string out = "'";
    for (char c : s) {
        if (c == '\'') out += "'\\''";
        else out.push_back(c);
    }
    out.push_back('\'');
    return out;
}

static std::vector<pid_t> load_victim_pids_from_finder(const std::string& ns,
                                                       const std::string& pod,
                                                       const std::string& finder_path) {
    std::vector<std::string> candidates;
    if (!finder_path.empty()) candidates.push_back(finder_path);
    if (const char* env = std::getenv("MCOZ_CGROUP_FINDER"); env && *env) {
        candidates.push_back(env);
    }
    candidates.push_back("/opt/mcoz/bin/cgroup_finder.sh");
    candidates.push_back("/opt/mcoz/evaluation/sig_vs_ghost/signal/scripts/cgroup_finder.sh");
    candidates.push_back("/home/yunni/mcoz/mcoz/m-coz/evaluation/sig_vs_ghost/signal/scripts/cgroup_finder.sh");
    candidates.push_back("/home/yunni/mcoz/mcoz/m-coz/evaluation/sig_vs_ghost/signal/script/cgroup_finder.sh");

    std::string script;
    for (const auto& c : candidates) {
        if (c.empty()) continue;
        if (access(c.c_str(), X_OK) == 0) {
            script = c;
            break;
        }
    }
    if (script.empty()) {
        std::cerr << "[ERROR] cgroup_finder not found/executable; set --victim-finder or MCOZ_CGROUP_FINDER\n";
        return {};
    }

    std::string cmd = shell_quote(script) + " " + shell_quote(pod) + " " + shell_quote(ns) + " 2>/dev/null";
    FILE* fp = popen(cmd.c_str(), "r");
    if (!fp) return {};

    std::vector<pid_t> pids;
    char line[512];
    while (fgets(line, sizeof(line), fp)) {
        std::string s = trim(line);
        if (s.empty()) continue;
        if (std::all_of(s.begin(), s.end(), [](unsigned char ch) { return std::isdigit(ch) != 0; })) {
            long v = std::strtol(s.c_str(), nullptr, 10);
            if (v > 0) pids.push_back(static_cast<pid_t>(v));
        }
    }
    pclose(fp);

    if (!pids.empty()) {
        std::sort(pids.begin(), pids.end());
        pids.erase(std::unique(pids.begin(), pids.end()), pids.end());
    }
    std::cerr << "[INFO] cgroup_finder victim pids: script=" << script
              << " ns/pod=" << ns << "/" << pod
              << " count=" << pids.size() << "\n";
    return pids;
}

static std::string get_pod_node(const std::string& ns, const std::string& pod) {
    std::string cmd = "kubectl get pod " + pod + " -n " + ns + kc_arg +
                      " -o jsonpath={.spec.nodeName} 2>/dev/null";
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
    // quiet debug prints

    const char* target_pod = nullptr;
    double speedup = 0.1; // 일단은 하드코딩 되어있음
    int period_ms = 5;
    uint64_t fixed_delay_ns = 0;
    bool request_aware = false;
    bool exact_mode = false;
    bool request_credit = false;
    bool refund_on_fail = false;
    bool enable_read_hook = false;
    std::string victim_pids_csv;
    std::string victim_finder_path;

    signal(SIGINT, sigint_handler);  // Ctrl-C 누르면 poll 루프 탈출
    atexit(cleanup);                 // 어떤 경로든 종료 시 해동 보장   

    static struct option opts[] = {
        {"target-pod", required_argument, 0, 't'},
        {"speedup", required_argument, 0, 's'},
        {"victim-pids", required_argument, 0, 'p'},
        {"fixed-delay-ns", required_argument, 0, 'd'},
        {"period-ms", required_argument, 0, 'm'},
        {"victim-finder", required_argument, 0, 'f'},
        {"request-aware", no_argument, 0, 'r'},
        {"exact-mode", no_argument, 0, 'x'},
        {"request-credit", no_argument, 0, 'R'},
        {"no-refund-on-fail", no_argument, 0, 'N'},
        {"enable-read-hook", no_argument, 0, 'E'},
        {0,0,0,0}
    };

    int opt;
    // parameter 파싱
    while((opt = getopt_long(argc, argv, "t:s:p:d:m:f:rxRNE", opts, nullptr)) != -1) {
        switch(opt) {
        case 't': target_pod = optarg; break;
        case 's': speedup = atof(optarg); break;
        case 'p': victim_pids_csv = optarg ? optarg : ""; break;
        case 'd':
            if (!parse_uint64(optarg ? optarg : "", fixed_delay_ns)) {
                std::cerr << "error: invalid --fixed-delay-ns value\n";
                return 1;
            }
            break;
        case 'm':
            period_ms = std::max(1, atoi(optarg ? optarg : "5"));
            break;
        case 'f':
            victim_finder_path = optarg ? optarg : "";
            break;
        case 'r':
            request_aware = true;
            break;
        case 'x':
            exact_mode = true;
            break;
        case 'R':
            request_credit = true;
            break;
        case 'N':
            refund_on_fail = false;
            break;
        case 'E':
            enable_read_hook = true;
            break;
        }
    }

    if (exact_mode) {
        request_aware = true;
    }

    if (request_credit) {
        if (fixed_delay_ns == 0) fixed_delay_ns = 10000000ULL; // default: 10ms
        std::cerr << "[INFO] request-credit mode selected: default_delay_ns=" << fixed_delay_ns
                  << " refund_on_fail=" << (refund_on_fail ? 1 : 0)
                  << " enable_read_hook=" << (enable_read_hook ? 1 : 0) << "\n";
        return request_credit_daemon(fixed_delay_ns, refund_on_fail, enable_read_hook);
    }

    // 디버깅 메시지 : parameter 로그
    // {
    //     std::ostringstream oss;
    //     oss << "[coz-daemon] start args | target="
    //         << (target_pod ? target_pod : "-")
    //         << " speedup=" << speedup
    //         << " protect=";
    //     if (protect_list.empty()) oss << "-";
    //     else {
    //         for (size_t i = 0; i < protect_list.size(); ++i) {
    //             if (i) oss << ",";
    //             oss << protect_list[i];
    //         }
    //     }
    //     if (!protect_cpus_str.empty()) oss << " protectCpus=" << protect_cpus_str;
    //     if (!others_cpus_str.empty())  oss << " othersCpus="  << others_cpus_str;
    //     oss << " isolateCores=" << (isolate_cores ? 1 : 0);
    //     std::cerr << oss.str() << std::endl;
    // }

    // target pod가 아예 없다면 종료
    if(!target_pod) {
        std::cerr << "need --target-pod" << std::endl;
        return 1;
    }

    try {
        std::vector<pid_t> manual_victim_pids;
        if (!victim_pids_csv.empty()) {
            manual_victim_pids = parse_victim_pid_csv(victim_pids_csv);
            if (manual_victim_pids.empty()) {
                std::cerr << "error: --victim-pids provided but no valid pid found: "
                          << victim_pids_csv << std::endl;
                return 1;
            }
            size_t n = set_victim_pids(manual_victim_pids);
            std::cerr << "[INFO] using manual victim pids: count=" << n
                      << " values=" << victim_pids_csv << "\n";
        }

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

        if (fixed_delay_ns > 0 && manual_victim_pids.empty()) {
            auto auto_pids = load_victim_pids_from_finder(ns, pod, victim_finder_path);
            if (auto_pids.empty()) {
                std::cerr << "[ERROR] fixed-delay mode requires victim pids; finder returned none\n";
                return 1;
            }
            size_t n = set_victim_pids(auto_pids);
            std::cerr << "[INFO] fixed-delay victim pids loaded: count=" << n << "\n";
            manual_victim_pids = std::move(auto_pids);
        }

        const char* my_node = std::getenv("NODE_NAME");
        std::string target_node = get_pod_node(ns, pod);

        bool target_local = (!my_node || target_node.empty() || target_node == my_node);

        if (request_aware && fixed_delay_ns == 0) {
            std::cerr << "error: --request-aware requires --fixed-delay-ns\n";
            return 1;
        }

        if (target_local) {
            if (request_aware) {
                std::cerr << "[INFO] target on same node; running request-aware mode" << std::endl;
            } else if (fixed_delay_ns > 0) {
                std::cerr << "[INFO] target on same node; running fixed-delay mode" << std::endl;
            } else {
                std::cerr << "[INFO] target on same node; running perf" << std::endl;
            }
            cgroup tgt = resolve_target_cgroup(tp);
            // On cgroup v1, perf_event controller requires an fd from the perf_event hierarchy.
            // If the discovered path is from another controller (e.g., freezer), remap to perf_event when available.
            std::string tgt_path = tgt.path;
            bool cgv2 = (access("/sys/fs/cgroup/cgroup.controllers", F_OK) == 0);
            if (!cgv2 && tgt_path.rfind("/sys/fs/cgroup/perf_event", 0) != 0) {
                const std::string base = "/sys/fs/cgroup";
                if (tgt_path.rfind(base + "/", 0) == 0) {
                    size_t next = tgt_path.find('/', base.size() + 1);
                    if (next != std::string::npos) {
                        std::string suffix = tgt_path.substr(next); // includes slash
                        std::string alt = base + "/perf_event" + suffix;
                        if (access(alt.c_str(), F_OK) == 0) {
                            std::cerr << "[INFO] cgroup v1: remapped target from " << tgt_path
                                      << " to " << alt << "\n";
                            tgt_path = alt;
                        } else {
                            std::cerr << "[WARN] cgroup v1: perf_event hierarchy path missing at " << alt
                                      << "; perf_event_open may fail. Consider mounting it: \n"
                                      << "       mount -t cgroup -o perf_event cgroup /sys/fs/cgroup/perf_event\n";
                        }
                    }
                }
            }
        // quiet
        int cg_fd = open(tgt_path.c_str(), O_DIRECTORY);
        if(cg_fd < 0) { perror("open cgroup"); return 1; }
        std::vector<cgroup> others;
        if (manual_victim_pids.empty()) {
            others = wait_for_victims(tgt, tp);
        }

        if (request_aware) {
            std::thread delay_th([others] {
                delay_consumer_only(others);
            });
            delay_th.detach();
            perf_sampler_request_aware(cg_fd,
                                       std::chrono::milliseconds(period_ms),
                                       speedup,
                                       fixed_delay_ns,
                                       others,
                                       "freezer");
        } else if (fixed_delay_ns > 0) {
            fixed_delay_injector(std::chrono::milliseconds(period_ms), fixed_delay_ns, others);
        } else {
            // Always run delay receiver; perf thread will send delay to all peers (including self).
            std::thread delay_th([others] {
                delay_consumer_only(others);
            });
            delay_th.detach();
            perf_sampler_sync(cg_fd, std::chrono::milliseconds(period_ms), speedup, others, "freezer");
        }
        } else {
            // Different node: don't do perf; listen for incoming delay and apply locally
            if (request_aware) {
                std::cerr << "[INFO] target on different node; request-aware mode consumer-only\n";
                std::vector<cgroup> none;
                delay_consumer_only(none);
            } else if (fixed_delay_ns > 0) {
                std::cerr << "[INFO] target on different node; fixed-delay mode consumer-only\n";
                std::vector<cgroup> none;
                delay_consumer_only(none);
            } else {
                std::cerr << "[INFO] target on different node; running consumer-only" << std::endl;
                std::vector<cgroup> others;
                if (manual_victim_pids.empty()) {
                    cgroup dummy{"", 0};
                    others = wait_for_victims(dummy, tp);
                }
                delay_consumer_only(others);
            }
        }
        
    } catch(const std::exception& e) {
        std::cerr << "error: " << e.what() << std::endl;
        return 1;
    }
    return 0;
}
