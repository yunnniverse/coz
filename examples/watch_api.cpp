// watcher.cpp
// Build: g++ -O2 -std=c++17 watcher.cpp -o watcher -lcurl -lssl -lcrypto -pthread

#include <iostream>
#include <sstream>
#include <string>
#include <fstream>
#include <chrono>
#include <thread>
#include <cstdlib>
#include <ctime>
#include <optional>
#include <cstdio>
#include <cinttypes>

#include <curl/curl.h>
#include "json.hpp"
using json = nlohmann::json;

// ---------- utils ----------
static std::string read_file(const std::string& path) {
    std::ifstream ifs(path);
    std::stringstream ss;
    ss << ifs.rdbuf();
    return ss.str();
}

static std::string rfc3339_now() {
    using namespace std::chrono;
    auto now = system_clock::now();
    auto secs = time_point_cast<seconds>(now);
    std::time_t t = system_clock::to_time_t(secs);
    std::tm tm{};
    gmtime_r(&t, &tm);
    char buf[64];
    std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tm);
    return std::string(buf);
}

static std::string read_ns_from_sa() {
    std::ifstream ifs("/var/run/secrets/kubernetes.io/serviceaccount/namespace");
    std::string ns;
    std::getline(ifs, ns);
    if (ns.empty()) ns = "default";
    return ns;
}

static inline long long now_wall_ns() {
    using namespace std::chrono;
    return duration_cast<nanoseconds>(system_clock::now().time_since_epoch()).count();
}

struct KubeEnv {
    std::string host;     // KUBERNETES_SERVICE_HOST
    std::string port;     // KUBERNETES_SERVICE_PORT
    std::string token;    // SA token content
    std::string ca;       // path to ca.crt
    std::string group;    // e.g., example.com
    std::string version;  // e.g., v1alpha1
    std::string plural;   // e.g., globaldelays
    std::string ns;       // "-" for cluster-scoped
};

static std::string env_or(const char* k, const std::string& defv) {
    const char* v = std::getenv(k);
    return v ? std::string(v) : defv;
}

static KubeEnv load_kubeenv() {
    KubeEnv k;
    k.host    = env_or("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc");
    k.port    = env_or("KUBERNETES_SERVICE_PORT", "443");
    k.token   = read_file("/var/run/secrets/kubernetes.io/serviceaccount/token");
    k.ca      = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";
    k.group   = env_or("KUBE_GROUP",   "example.com");
    k.version = env_or("KUBE_VERSION", "v1alpha1");
    k.plural  = env_or("KUBE_PLURAL",  "globaldelays"); // 기본값 plural 수정
    std::string ns = env_or("CR_NAMESPACE", "");
    if (ns.empty()) ns = read_ns_from_sa();
    k.ns = ns; // "-"면 cluster-scoped
    return k;
}

// --- URL builder: 컬렉션 watch + fieldSelector(권장) 또는 단일 객체 watch 선택 가능 ---
static std::string build_watch_url(const KubeEnv& k,
                                   const std::string& name,
                                   const std::string& start_rv,
                                   bool collection_mode = true) {
    std::string base = "https://" + k.host + ":" + k.port + "/apis/" + k.group + "/" + k.version;
    std::string url;
    if (collection_mode) {
        if (k.ns == "-") url = base + "/" + k.plural;
        else             url = base + "/namespaces/" + k.ns + "/" + k.plural;
        url += "?watch=true&allowWatchBookmarks=true&fieldSelector=metadata.name=" + name;
    } else {
        if (k.ns == "-") url = base + "/" + k.plural + "/" + name;
        else             url = base + "/namespaces/" + k.ns + "/" + k.plural + "/" + name;
        url += "?watch=true&allowWatchBookmarks=true";
    }
    if (!start_rv.empty()) url += "&resourceVersion=" + start_rv;
    else                   url += "&resourceVersion=0";
    return url;
}

// ---------- curl stream ----------
struct CurlStream {
    CURL* curl{nullptr};
    struct curl_slist* headers{nullptr};
    std::string partial; // carry-over for split lines
    long http_code{0};
    std::optional<std::string> last_rv; // updated inside callback
};

static void handle_line(CurlStream* cs, const std::string& line) {
    if (line.empty()) return;

    // watch event 찍기
    const long long event_ns = now_wall_ns();  // ★ watch 이벤트가 우리 쪽에 도착한 시간(ns)

    try {
        json ev = json::parse(line);
        std::string type = ev.value("type", "");

        if (type == "BOOKMARK") {
            if (ev.contains("object") && ev["object"].contains("metadata")) {
                auto rv = ev["object"]["metadata"].value("resourceVersion", "");
                if (!rv.empty()) cs->last_rv = rv;
            }
            return;
        }
        if (type == "ERROR") {
            int code = 0;
            if (ev.contains("object") && ev["object"].contains("code")) {
                code = ev["object"]["code"].get<int>();
            }
            std::cerr << "[" << rfc3339_now() << "] [ERROR event] code=" << code
                      << " body=" << line << '\n';
            if (code == 410) cs->last_rv = std::nullopt;
            return;
        }

        // ADDED / MODIFIED / DELETED
        if (ev.contains("object")) {
            auto obj = ev["object"];
            std::string rv = obj["metadata"].value("resourceVersion", "");
            if (!rv.empty()) cs->last_rv = rv;

            // 요청: 업데이트마다 짧은 로그만
            if (obj.contains("spec")) {
                long long value = 0LL, ver = 0LL;
                auto spec = obj["spec"];
                if (spec.contains("value"))   value = spec["value"].get<long long>();
                if (spec.contains("version")) ver   = spec["version"].get<long long>();
                // ADDED/MODIFIED/DELETED 상관없이 찍고 싶다면 그대로 호출
                // (원한다면 type=="MODIFIED" 등 조건을 더 걸어도 됨)
                
                // ★ 요청 포맷: [RFC3339] value : # version : # event_ns=#
                std::cout << "[watcher] version : " << ver
                        << " value : "   << value
                        << " event_ns : " << event_ns
                        << '\n';
            }
        }
    } catch (const std::exception& e) {
        std::cerr << "[" << rfc3339_now() << "] [WARN] Bad line: " << e.what()
                  << " line=" << line << '\n';
    }
}

static size_t write_cb(void* contents, size_t size, size_t nmemb, void* userp) {
    auto* cs = reinterpret_cast<CurlStream*>(userp);
    const size_t len = size * nmemb;
    const char* ptr = static_cast<const char*>(contents);

    cs->partial.append(ptr, len);

    size_t pos = 0;
    while (true) {
        size_t nl = cs->partial.find('\n', pos);
        if (nl == std::string::npos) break;
        std::string line = cs->partial.substr(pos, nl - pos);
        handle_line(cs, line);
        pos = nl + 1;
    }
    if (pos > 0) cs->partial.erase(0, pos);
    return len;
}

// ---------- watch loop ----------
static std::optional<std::string> run_watch_once(const KubeEnv& k,
                                                 const std::string& name,
                                                 const std::string& start_rv) {
    // 기본: 컬렉션 watch + fieldSelector (실시간성/견고성↑)
    const bool collection_mode = true;
    // watch url 설정
    const std::string url = build_watch_url(k, name, start_rv, collection_mode);

    CurlStream cs;
    // context 생성
    cs.curl = curl_easy_init();
    if (!cs.curl) {
        std::cerr << "[" << rfc3339_now() << "] [watch] curl init failed\n";
        return std::nullopt;
    }

    std::string auth = "Authorization: Bearer " + k.token;
    cs.headers = curl_slist_append(cs.headers, auth.c_str());
    cs.headers = curl_slist_append(cs.headers, "Accept: application/json;stream=watch");

    curl_easy_setopt(cs.curl, CURLOPT_HTTPHEADER, cs.headers);
    curl_easy_setopt(cs.curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(cs.curl, CURLOPT_SSLCERTTYPE, "PEM");
    curl_easy_setopt(cs.curl, CURLOPT_CAINFO, k.ca.c_str());
    curl_easy_setopt(cs.curl, CURLOPT_SSL_VERIFYPEER, 1L);
    curl_easy_setopt(cs.curl, CURLOPT_SSL_VERIFYHOST, 2L);

    // streaming callback
    curl_easy_setopt(cs.curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(cs.curl, CURLOPT_WRITEDATA, &cs);

    // long-lived connection
    curl_easy_setopt(cs.curl, CURLOPT_TIMEOUT, 0L);
    curl_easy_setopt(cs.curl, CURLOPT_ACCEPT_ENCODING, ""); // enable gzip if server supports

    // TCP keepalive
    curl_easy_setopt(cs.curl, CURLOPT_TCP_KEEPALIVE, 1L);
    curl_easy_setopt(cs.curl, CURLOPT_TCP_KEEPIDLE, 30L);
    curl_easy_setopt(cs.curl, CURLOPT_TCP_KEEPINTVL, 15L);

    std::cerr << "[" << rfc3339_now() << "] opening: " << url << '\n';

    CURLcode rc = curl_easy_perform(cs.curl);
    curl_easy_getinfo(cs.curl, CURLINFO_RESPONSE_CODE, &cs.http_code);

    if (rc != CURLE_OK) {
        std::cerr << "[" << rfc3339_now() << "] [WARN] curl error: "
                  << curl_easy_strerror(rc) << " http=" << cs.http_code << '\n';
    } else if (cs.http_code < 200 || cs.http_code >= 300) {
        std::cerr << "[" << rfc3339_now() << "] [HTTP " << cs.http_code << "] body=" << cs.partial << '\n';
    }

    if (cs.headers) curl_slist_free_all(cs.headers);
    if (cs.curl) curl_easy_cleanup(cs.curl);
    return cs.last_rv; // may be nullopt
}

int main() {
    // ---- 출력 즉시 flush 설정 (중요!) ----
    std::ios::sync_with_stdio(false);
    std::cout.setf(std::ios::unitbuf);
    std::cerr.setf(std::ios::unitbuf);
    setvbuf(stdout, nullptr, _IOLBF, 0); // 줄버퍼
    setvbuf(stderr, nullptr, _IONBF, 0); // 무버퍼

    curl_global_init(CURL_GLOBAL_DEFAULT);
    auto k = load_kubeenv();

    const char* name_env = std::getenv("GLOBAL_DELAY_NAME");
    std::string name = name_env ? name_env : "global-delay";

    std::string rv; // empty → start at latest via resourceVersion=0
    // khan : 줄여서
    int backoff_ms = 100;

    while (true) {
        auto new_rv = run_watch_once(k, name, rv);
        if (!new_rv.has_value()) {
            rv.clear(); // reset to 0 next time
            std::this_thread::sleep_for(std::chrono::milliseconds(backoff_ms));
            backoff_ms = std::min(backoff_ms * 2, 8000);
        } else {
            rv = *new_rv;
            backoff_ms = 500;
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
    }

    curl_global_cleanup();
    return 0;
}
