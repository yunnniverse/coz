// // Simple updater for GlobalDelay CR
// // - GET /update?add=100  -> increase value
// // - GET /update?minus=50 -> decrease value
// //
// // Build deps: libcurl, OpenSSL (for HTTPS), pthread
// // HTTP server: cpp-httplib (header-only)
// // JSON: nlohmann/json (header-only)

// #include <iostream>
// #include <fstream>
// #include <sstream>
// #include <string>
// #include <ctime>
// #include <chrono>
// #include <thread>
// #include <optional>
// #include <cinttypes>
// #include <cstdio>

// #include <curl/curl.h>
// #define CPPHTTPLIB_OPENSSL_SUPPORT
// #include "httplib.h"
// #include "json.hpp"

// using json = nlohmann::json;

// // --- util: read whole file ---
// static std::string read_file(const std::string& path) {
//     std::ifstream ifs(path);
//     if (!ifs.is_open()) return "";
//     std::stringstream ss; ss << ifs.rdbuf();
//     return ss.str();
// }

// static inline long long now_wall_ns() {
//     using namespace std::chrono;
//     return duration_cast<nanoseconds>(system_clock::now().time_since_epoch()).count();
// }

// struct KubeEnv {
//     std::string host;   // e.g., 10.96.0.1
//     std::string port;   // e.g., 443
//     std::string token;  // SA token
//     std::string ca;     // CA cert path
//     std::string base;   // https://HOST:PORT/apis/example.com/v1alpha1
// };

// static KubeEnv load_kubeenv() {
//     KubeEnv k;
//     const char* h = std::getenv("KUBERNETES_SERVICE_HOST");
//     const char* p = std::getenv("KUBERNETES_SERVICE_PORT");
//     k.host = h ? h : "kubernetes.default.svc";
//     k.port = p ? p : "443";
//     k.token = read_file("/var/run/secrets/kubernetes.io/serviceaccount/token");
//     k.ca = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";
//     k.base = "https://" + k.host + ":" + k.port + "/apis/example.com/v1alpha1";
//     return k;
// }

// // libcurl helpers
// static size_t write_cb(void* contents, size_t size, size_t nmemb, void* userp) {
//     ((std::string*)userp)->append((char*)contents, size * nmemb);
//     return size * nmemb;
// }

// struct HttpResp {
//     long code{0};
//     std::string body;
//     std::string err;
// };

// static HttpResp http_get(const KubeEnv& k, const std::string& url) {
//     HttpResp r;
//     CURL* curl = curl_easy_init();
//     if (!curl) { r.err = "curl init failed"; return r; }
//     struct curl_slist* headers = nullptr;
//     std::string auth = "Authorization: Bearer " + k.token;
//     headers = curl_slist_append(headers, auth.c_str());
//     headers = curl_slist_append(headers, "Accept: application/json");
//     curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
//     curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
//     curl_easy_setopt(curl, CURLOPT_SSLCERTTYPE, "PEM");
//     curl_easy_setopt(curl, CURLOPT_CAINFO, k.ca.c_str());
//     curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
//     curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
//     curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
//     curl_easy_setopt(curl, CURLOPT_WRITEDATA, &r.body);
//     CURLcode res = curl_easy_perform(curl);
//     if (res != CURLE_OK) r.err = curl_easy_strerror(res);
//     curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &r.code);
//     curl_slist_free_all(headers);
//     curl_easy_cleanup(curl);
//     return r;
// }

// static HttpResp http_put_json(const KubeEnv& k, const std::string& url, const std::string& body) {
//     HttpResp r;
//     CURL* curl = curl_easy_init();
//     if (!curl) { r.err = "curl init failed"; return r; }
//     struct curl_slist* headers = nullptr;
//     std::string auth = "Authorization: Bearer " + k.token;
//     headers = curl_slist_append(headers, auth.c_str());
//     headers = curl_slist_append(headers, "Content-Type: application/json");
//     headers = curl_slist_append(headers, "Accept: application/json");
//     curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
//     curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
//     curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, "PUT");
//     curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());
//     curl_easy_setopt(curl, CURLOPT_SSLCERTTYPE, "PEM");
//     curl_easy_setopt(curl, CURLOPT_CAINFO, k.ca.c_str());
//     curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
//     curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
//     curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
//     curl_easy_setopt(curl, CURLOPT_WRITEDATA, &r.body);
//     CURLcode res = curl_easy_perform(curl);
//     if (res != CURLE_OK) r.err = curl_easy_strerror(res);
//     curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &r.code);
//     curl_slist_free_all(headers);
//     curl_easy_cleanup(curl);
//     return r;
// }

// struct GlobalDelay {
//     int64_t value{0};
//     int64_t version{0};
//     std::string resourceVersion; // metadata.resourceVersion
//     json full; // whole object for PUT
// };

// // 기존 리소스에서 값을 가져옴
// static std::optional<GlobalDelay> fetch_globaldelay(const KubeEnv& k, const std::string& name) {
//     std::string url = k.base + "/globaldelays/" + name;
//     auto r = http_get(k, url);
//     if (r.code != 200) {
//         std::cerr << "GET failed: " << r.code << " " << r.err << " body=" << r.body << std::endl;
//         return std::nullopt;
//     }
//     auto j = json::parse(r.body);
//     GlobalDelay gd;
//     gd.full = j;
//     gd.value = j["spec"].value("value", 0);
//     gd.version = j["spec"].value("version", 0);
//     gd.resourceVersion = j["metadata"].value("resourceVersion", "");
//     return gd;
// }

// // 기존 리소스의 갱신
// static HttpResp put_globaldelay(const KubeEnv& k, const GlobalDelay& gd, const std::string& name) {
//     json j = gd.full;
//     j["spec"]["value"] = gd.value;
//     j["spec"]["version"] = gd.version;
//     // keep metadata.resourceVersion to enforce optimistic concurrency
//     j["metadata"]["resourceVersion"] = gd.resourceVersion;
//     std::string url = k.base + "/globaldelays/" + name;
//     return http_put_json(k, url, j.dump());
// }

// int main() {
//     std::ios::sync_with_stdio(false);
//     std::cout.setf(std::ios::unitbuf);
//     std::cerr.setf(std::ios::unitbuf);
//     setvbuf(stdout, nullptr, _IOLBF, 0); // 줄 버퍼
//     setvbuf(stderr, nullptr, _IONBF, 0); // 무버퍼

//     KubeEnv k = load_kubeenv();
//     const std::string cr_name = std::getenv("GLOBAL_DELAY_NAME") ? std::getenv("GLOBAL_DELAY_NAME") : "global-delay";
//     const int max_retries = 5;

//     httplib::Server svr;
//     svr.Get("/healthz", [](const httplib::Request&, httplib::Response& res) {
//         res.set_content("ok\n", "text/plain");
//     });

//     svr.Get("/update", [&](const httplib::Request& req, httplib::Response& res) {
//         try {
//             // watch event 찍기
//             const long long event_ns = now_wall_ns();  // ★ watch 이벤트가 우리 쪽에 도착한 시간(ns)   
        
//             // ====== NEW: 요청 시각 기록 ======
//             auto t_req = std::chrono::steady_clock::now();

//             int64_t delta = 0;
//             if (req.has_param("add")) {
//                 delta = std::stoll(req.get_param_value("add"));
//             } else if (req.has_param("minus")) {
//                 delta = -std::stoll(req.get_param_value("minus"));
//             } else {
//                 res.status = 400;
//                 res.set_content(R"({"error":"use ?add=N or ?minus=N"})", "application/json");
//                 return;
//             }

//             int attempt = 0;
//             for (; attempt < max_retries; ++attempt) {
//                 // 기존 리소스 값을 가져옴
//                 auto cur = fetch_globaldelay(k, cr_name);
//                 if (!cur) {
//                     res.status = 502;
//                     res.set_content(R"({"error":"failed to fetch CR"})", "application/json");
//                     return;
//                 }

//                 // 갱신할 값들 생성 -> 이 부분은 사실 필요가 없을 듯
//                 GlobalDelay next = *cur;
//                 next.value = cur->value + delta;
//                 next.version = cur->version + 1; // monotonic snapshot version

                
//                 auto putr = put_globaldelay(k, next, cr_name);
//                 if (putr.code == 200) {
//                     // ====== NEW: APIServer 적용 완료 시각 기록 ======
//                     auto t_put_ok = std::chrono::steady_clock::now();
//                     auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(t_put_ok - t_req).count();

//                     // APIServer가 반환한 최신 오브젝트에서 resourceVersion 파싱
//                     json updated = json::parse(putr.body);
//                     std::string rv = "0";
//                     try {
//                         // 보통 문자열 형태로 옴
//                         rv = updated["metadata"]["resourceVersion"].get<std::string>();
//                     } catch (...) {
//                         // 혹시 정수로 오면 문자열로 변환
//                         if (updated["metadata"].contains("resourceVersion")) {
//                             rv = std::to_string(updated["metadata"]["resourceVersion"].get<long long>());
//                         }
//                     }
                    
//                     // ★ 요청 포맷: [RFC3339] value : # version : # event_ns=#
//                     std::cout << "[updater] version : " << next.version 
//                           << " value : "  << next.value
//                           << " req_ns : " << event_ns
//                           << '\n';

//                     // 응답 JSON 구성
//                     json out = {
//                         {"value", next.value},
//                         {"version", next.version},
//                         // 요청을 보내고 ~ 받기까지 걸린 시간 (cr 반영 완료 후 응답 return)
//                         // {"latency_api_ms", ms},
//                         // 요청이 들어온 순간 
//                         {"req_ns", event_ns},
//                     };
//                     res.status = 200;
//                     res.set_content(out.dump() + "\n", "application/json");
//                     return;
//                 } else if (putr.code == 409) {
//                     // conflict -> 누군가가 먼저 갱신함. 잠깐 대기 후 재시도
//                     std::this_thread::sleep_for(std::chrono::milliseconds(20));
//                     continue;
//                 } else {
//                     json err = {
//                         {"error", "PUT failed"},
//                         {"code", putr.code},
//                         {"body", putr.body}
//                     };
//                     res.status = 502;
//                     res.set_content(err.dump() + "\n", "application/json");
//                     return;
//                 }
//             }
//             res.status = 409;
//             res.set_content(R"({"error":"conflict after retries"})", "application/json");
//         } catch (const std::exception& e) {
//             json err = {{"error", std::string("exception: ") + e.what()}};
//             res.status = 500;
//             res.set_content(err.dump() + "\n", "application/json");
//         }
//     });

//     std::cout << "Listening on 0.0.0.0:8080\n";
//     svr.listen("0.0.0.0", 8080);
//     return 0;
// }


// updater_with_cache.cpp
// Pattern: 초기 1회 GET으로 캐시 시드 → 이후엔 캐시 기반 PUT만 (409시 GET으로 동기화 후 재시도)
//
// Build deps: libcurl, OpenSSL (for HTTPS), pthread
// HTTP server: cpp-httplib (header-only)
// JSON: nlohmann/json (header-only)

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <ctime>
#include <chrono>
#include <thread>
#include <optional>
#include <cinttypes>
#include <cstdio>
#include <mutex>

#include <curl/curl.h>
#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"
#include "json.hpp"

using json = nlohmann::json;

// --- util: read whole file ---
static std::string read_file(const std::string& path) {
    std::ifstream ifs(path);
    if (!ifs.is_open()) return "";
    std::stringstream ss; ss << ifs.rdbuf();
    return ss.str();
}

static inline long long now_wall_ns() {
    using namespace std::chrono;
    return duration_cast<nanoseconds>(system_clock::now().time_since_epoch()).count();
}

struct KubeEnv {
    std::string host;   // e.g., 10.96.0.1
    std::string port;   // e.g., 443
    std::string token;  // SA token
    std::string ca;     // CA cert path
    std::string base;   // https://HOST:PORT/apis/example.com/v1alpha1
};

static KubeEnv load_kubeenv() {
    KubeEnv k;
    const char* h = std::getenv("KUBERNETES_SERVICE_HOST");
    const char* p = std::getenv("KUBERNETES_SERVICE_PORT");
    k.host = h ? h : "kubernetes.default.svc";
    k.port = p ? p : "443";
    k.token = read_file("/var/run/secrets/kubernetes.io/serviceaccount/token");
    k.ca = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";
    k.base = "https://" + k.host + ":" + k.port + "/apis/example.com/v1alpha1";
    return k;
}

// libcurl helpers
static size_t write_cb(void* contents, size_t size, size_t nmemb, void* userp) {
    ((std::string*)userp)->append((char*)contents, size * nmemb);
    return size * nmemb;
}

struct HttpResp {
    long code{0};
    std::string body;
    std::string err;
};

static HttpResp http_get(const KubeEnv& k, const std::string& url) {
    HttpResp r;
    CURL* curl = curl_easy_init();
    if (!curl) { r.err = "curl init failed"; return r; }
    struct curl_slist* headers = nullptr;
    std::string auth = "Authorization: Bearer " + k.token;
    headers = curl_slist_append(headers, auth.c_str());
    headers = curl_slist_append(headers, "Accept: application/json");
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_SSLCERTTYPE, "PEM");
    curl_easy_setopt(curl, CURLOPT_CAINFO, k.ca.c_str());
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &r.body);
    CURLcode res = curl_easy_perform(curl);
    if (res != CURLE_OK) r.err = curl_easy_strerror(res);
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &r.code);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    return r;
}

static HttpResp http_put_json(const KubeEnv& k, const std::string& url, const std::string& body) {
    HttpResp r;
    CURL* curl = curl_easy_init();
    if (!curl) { r.err = "curl init failed"; return r; }
    struct curl_slist* headers = nullptr;
    std::string auth = "Authorization: Bearer " + k.token;
    headers = curl_slist_append(headers, auth.c_str());
    headers = curl_slist_append(headers, "Content-Type: application/json");
    headers = curl_slist_append(headers, "Accept: application/json");
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, "PUT");
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());
    curl_easy_setopt(curl, CURLOPT_SSLCERTTYPE, "PEM");
    curl_easy_setopt(curl, CURLOPT_CAINFO, k.ca.c_str());
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &r.body);
    CURLcode res = curl_easy_perform(curl);
    if (res != CURLE_OK) r.err = curl_easy_strerror(res);
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &r.code);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    return r;
}

struct GlobalDelay {
    int64_t value{0};
    int64_t version{0};
    std::string resourceVersion; // metadata.resourceVersion
    json full; // whole object for PUT
};

// GET one CR
static std::optional<GlobalDelay> fetch_globaldelay(const KubeEnv& k, const std::string& name) {
    std::string url = k.base + "/globaldelays/" + name;
    auto r = http_get(k, url);
    if (r.code != 200) {
        std::cerr << "GET failed: " << r.code << " " << r.err << " body=" << r.body << std::endl;
        return std::nullopt;
    }
    auto j = json::parse(r.body);
    GlobalDelay gd;
    gd.full = j;
    gd.value = j["spec"].value("value", 0);
    gd.version = j["spec"].value("version", 0);
    gd.resourceVersion = j["metadata"].value("resourceVersion", "");
    return gd;
}

// PUT update CR (with optimistic lock)
static HttpResp put_globaldelay(const KubeEnv& k, const GlobalDelay& gd, const std::string& name) {
    json j = gd.full;
    j["spec"]["value"]   = gd.value;
    j["spec"]["version"] = gd.version;
    // keep metadata.resourceVersion to enforce optimistic concurrency
    j["metadata"]["resourceVersion"] = gd.resourceVersion;
    std::string url = k.base + "/globaldelays/" + name;
    return http_put_json(k, url, j.dump());
}

// ====== 캐시 ======
struct Cache {
    std::mutex mu;       // 프로세스 내 동시 요청 직렬화
    bool ready{false};   // 초기 시드 여부
    json full;
    int64_t value{0};
    int64_t version{0};
    std::string rv;      // metadata.resourceVersion
} g_cache;

// 캐시 시드/새로고침
static bool refresh_cache(const KubeEnv& k, const std::string& name) {
    auto cur = fetch_globaldelay(k, name);
    if (!cur) return false;
    g_cache.full   = cur->full;
    g_cache.value  = cur->value;
    g_cache.version= cur->version;
    g_cache.rv     = cur->resourceVersion;
    g_cache.ready  = true;
    return true;
}

// 캐시 기반 1회 업데이트 (성공 경로: PUT 1회)
// 409 발생 시: 캐시 새로고침(GET) 후 1회 재시도
static HttpResp update_with_cache(const KubeEnv& k, const std::string& name, int64_t delta) {
    std::lock_guard<std::mutex> lk(g_cache.mu); // 직렬화(중복충돌↓)
    if (!g_cache.ready) {
        if (!refresh_cache(k, name)) {
            return HttpResp{.code=502, .body=R"({"error":"initial cache refresh failed"})"};
        }
    }

    auto make_next_from_cache = [&]() -> GlobalDelay {
        GlobalDelay next;
        next.full            = g_cache.full;
        next.value           = g_cache.value + delta;
        next.version         = g_cache.version + 1; // 모니터링용 단순 카운터
        next.resourceVersion = g_cache.rv;          // 낙관적 락
        return next;
    };

    // 1) 1차 시도
    GlobalDelay next = make_next_from_cache();
    auto putr = put_globaldelay(k, next, name);
    if (putr.code == 200) {
        // 응답으로 캐시 갱신
        try {
            json updated = json::parse(putr.body);
            g_cache.full    = updated;
            g_cache.value   = updated["spec"].value("value",  next.value);
            g_cache.version = updated["spec"].value("version",next.version);
            // rv는 문자열/정수 두 케이스 처리
            try {
                g_cache.rv = updated["metadata"]["resourceVersion"].get<std::string>();
            } catch (...) {
                if (updated["metadata"].contains("resourceVersion")) {
                    g_cache.rv = std::to_string(updated["metadata"]["resourceVersion"].get<long long>());
                }
            }
        } catch (...) {
            // 파싱 실패해도 캐시를 next로 보수적으로 갱신
            g_cache.value   = next.value;
            g_cache.version = next.version;
        }
        return putr;
    }

    if (putr.code != 409) {
        return putr; // 기타 오류는 바로 반환
    }

    // 2) 409 발생: 캐시를 최신으로 동기화 후 한 번 더 시도
    if (!refresh_cache(k, name)) {
        return HttpResp{.code=502, .body=R"({"error":"refresh after 409 failed"})"};
    }
    next = make_next_from_cache();
    auto putr2 = put_globaldelay(k, next, name);
    if (putr2.code == 200) {
        try {
            json updated = json::parse(putr2.body);
            g_cache.full    = updated;
            g_cache.value   = updated["spec"].value("value",  next.value);
            g_cache.version = updated["spec"].value("version",next.version);
            try {
                g_cache.rv = updated["metadata"]["resourceVersion"].get<std::string>();
            } catch (...) {
                if (updated["metadata"].contains("resourceVersion")) {
                    g_cache.rv = std::to_string(updated["metadata"]["resourceVersion"].get<long long>());
                }
            }
        } catch (...) {
            g_cache.value   = next.value;
            g_cache.version = next.version;
        }
    }
    return putr2;
}

int main() {
    std::ios::sync_with_stdio(false);
    std::cout.setf(std::ios::unitbuf);
    std::cerr.setf(std::ios::unitbuf);
    setvbuf(stdout, nullptr, _IOLBF, 0); // 줄 버퍼
    setvbuf(stderr, nullptr, _IONBF, 0); // 무버퍼

    KubeEnv k = load_kubeenv();
    const std::string cr_name = std::getenv("GLOBAL_DELAY_NAME") ? std::getenv("GLOBAL_DELAY_NAME") : "global-delay";

    // 서버 시작 전에 1회 시드(실패해도 핸들러에서 다시 시도)
    if (!refresh_cache(k, cr_name)) {
        std::cerr << "[WARN] initial cache refresh failed; will retry on first request\n";
    }

    httplib::Server svr;
    svr.Get("/healthz", [](const httplib::Request&, httplib::Response& res) {
        res.set_content("ok\n", "text/plain");
    });

    svr.Get("/update", [&](const httplib::Request& req, httplib::Response& res) {
        try {
            const long long req_ns = now_wall_ns();  // 요청 도착 시각(ns)

            int64_t delta = 0;
            if (req.has_param("add")) {
                delta = std::stoll(req.get_param_value("add"));
            } else if (req.has_param("minus")) {
                delta = -std::stoll(req.get_param_value("minus"));
            } else {
                res.status = 400;
                res.set_content(R"({"error":"use ?add=N or ?minus=N"})", "application/json");
                return;
            }

            auto r = update_with_cache(k, cr_name, delta);
            if (r.code == 200) {
                // 최신 캐시 스냅샷을 읽어서 응답 구성
                int64_t out_value=0, out_version=0;
                {
                    std::lock_guard<std::mutex> lk(g_cache.mu);
                    out_value  = g_cache.value;
                    out_version= g_cache.version;
                }
                
                // API server가 200을 돌려준 시점
                const long long put_ns = now_wall_ns();  // ★ APIServer가 200을 돌려준 즉시 시각

                // 로깅 (요청 시각 기준)
                std::cout << "[updater] version : " << out_version
                          << " value : "  << out_value
                          << " req_ns : " << req_ns
                          << " put_ns : " << put_ns
                          << '\n';  

                json out = {
                    {"value",  out_value},
                    {"version",out_version},
                    {"req_ns", req_ns},
                    {"put_ns",put_ns}
                };
                res.status = 200;
                res.set_content(out.dump() + "\n", "application/json");
            } else if (r.code == 409) {
                res.status = 409;
                json out = {{"error","conflict after retry"}, {"body", r.body}};
                res.set_content(out.dump() + "\n", "application/json");
            } else {
                res.status = 502;
                json out = {{"error","PUT failed"}, {"code", r.code}, {"body", r.body}};
                res.set_content(out.dump() + "\n", "application/json");
            }
        } catch (const std::exception& e) {
            json err = {{"error", std::string("exception: ") + e.what()}};
            res.status = 500;
            res.set_content(err.dump() + "\n", "application/json");
        }
    });

    std::cout << "Listening on 0.0.0.0:8080\n";
    svr.listen("0.0.0.0", 8080);
    return 0;
}
