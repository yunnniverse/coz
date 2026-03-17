package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

type stats struct {
	Triggered         int64   `json:"triggered"`
	ArmedOK           int64   `json:"armed_ok"`
	ArmedFail         int64   `json:"armed_fail"`
	ArmSuppressed     int64   `json:"arm_suppressed"`
	ArmSuspendedCount int64   `json:"arm_suspended_count"`
	ArmSuspendUntil   float64 `json:"arm_suspend_until_unix"`
	ArmSuspendReason  string  `json:"arm_suspend_reason"`
	ArmActive         int64   `json:"arm_active"`
	ArmToggleCount    int64   `json:"arm_toggle_count"`
	DisabledSkip      int64   `json:"disabled_skip"`
	Skipped           int64   `json:"skipped"`
	ArmDelayNs        int64   `json:"arm_delay_ns"`
	ArmCount          int64   `json:"arm_count"`
	CreditBalanceReq  int64   `json:"credit_balance_req"`
	CreditMiss        int64   `json:"credit_miss"`
	RefillRequest     int64   `json:"refill_request"`
	RefillOK          int64   `json:"refill_ok"`
	RefillFail        int64   `json:"refill_fail"`
	RefillTokensAdded int64   `json:"refill_tokens_added"`
	RefillCreditsAdd  int64   `json:"refill_credits_added"`
	LastRefillMs      float64 `json:"last_refill_ms"`
	ConfigSkip        int64   `json:"config_skip"`
	LastError         string  `json:"last_error"`
	LastPath          string  `json:"last_path"`
	LastRequestID     string  `json:"last_request_id"`
}

type gateState struct {
	mu sync.Mutex

	armActive     bool
	armDelayNs    int64
	armCount      int64
	creditBalance int64

	st stats
}

var (
	port             = envInt("MCOZ_GATE_PORT", 19092)
	podName          = envStr("POD_NAME", hostName())
	podNamespace     = envStr("POD_NAMESPACE", "default")
	containerName    = envStr("MCOZ_CONTAINER", "app")
	sourceID         = envStr("MCOZ_SOURCE_ID", fmt.Sprintf("%s/%s/%s", podNamespace, podName, containerName))
	armURL           = envStr("MCOZ_ARM_URL", "http://coz-control-local.mcoz-system.svc.cluster.local:19091/arm")
	defaultDelayNs   = int64(envInt("MCOZ_DELAY_NS", 10_000_000))
	defaultCount     = int64(envInt("MCOZ_COUNT", 1))
	timeoutSec       = envFloat("MCOZ_ARM_TIMEOUT_SEC", 0.2)
	matchMode        = strings.ToLower(envStr("MCOZ_MATCH_MODE", "all"))
	matchHeader      = strings.ToLower(envStr("MCOZ_MATCH_HEADER", "x-mcoz-enable"))
	matchHeaderValue = envStr("MCOZ_MATCH_HEADER_VALUE", "1")
	matchPathPrefix  = envStr("MCOZ_MATCH_PATH_PREFIX", "")
	debugHeaders     = envBool("MCOZ_DEBUG_HEADERS", true)
	armActiveDefault = envBool("MCOZ_ARM_ACTIVE_DEFAULT", true)

	refillTargetReq = int64(envInt("MCOZ_REFILL_TARGET_REQ", 16))
	refillLowWater  = int64(envInt("MCOZ_REFILL_LOW_WATER_REQ", 4))
	refillBatchReq  = int64(envInt("MCOZ_REFILL_BATCH_REQ", 8))
	refillEnabled   = envBool("MCOZ_REFILL_ENABLED", true)
	sendOnZeroDelay = envBool("MCOZ_SEND_ON_ZERO_DELAY", true)
	syncRefillMiss  = envBool("MCOZ_SYNC_REFILL_ON_MISS", false)
	verboseEvents   = envBool("MCOZ_VERBOSE_EVENTS", false)

	g = &gateState{}

	armClient = &http.Client{
		Timeout: time.Duration(timeoutSec * float64(time.Second)),
		Transport: &http.Transport{
			MaxIdleConns:        128,
			MaxIdleConnsPerHost: 128,
			IdleConnTimeout:     90 * time.Second,
		},
	}

	refillCh = make(chan struct{}, 1)
)

type refillResult struct {
	Attempted      bool    `json:"attempted"`
	OK             bool    `json:"ok"`
	Reason         string  `json:"reason,omitempty"`
	StatusCode     int     `json:"status_code,omitempty"`
	ElapsedMs      float64 `json:"elapsed_ms,omitempty"`
	RequestedToken int64   `json:"requested_tokens,omitempty"`
	Balance        int64   `json:"balance,omitempty"`
}

func envStr(key, def string) string {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	return v
}

func envInt(key string, def int) int {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	i, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return i
}

func envFloat(key string, def float64) float64 {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		return def
	}
	return f
}

func envBool(key string, def bool) bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv(key)))
	if v == "" {
		return def
	}
	switch v {
	case "1", "true", "yes", "on", "y":
		return true
	case "0", "false", "no", "off", "n":
		return false
	default:
		return def
	}
}

func hostName() string {
	h, err := os.Hostname()
	if err != nil || strings.TrimSpace(h) == "" {
		return "unknown"
	}
	return h
}

func writeJSON(w http.ResponseWriter, code int, payload any) {
	raw, _ := json.Marshal(payload)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_, _ = w.Write(raw)
}

func writeText(w http.ResponseWriter, code int, body string, headers map[string]string) {
	for k, v := range headers {
		w.Header().Set(k, v)
	}
	w.Header().Set("Content-Type", "text/plain")
	w.WriteHeader(code)
	_, _ = w.Write([]byte(body))
}

func parseBoolPtr(raw string) (*bool, bool) {
	s := strings.ToLower(strings.TrimSpace(raw))
	switch s {
	case "1", "true", "yes", "on", "y":
		v := true
		return &v, true
	case "0", "false", "no", "off", "n":
		v := false
		return &v, true
	default:
		return nil, false
	}
}

func parseInt64Ptr(raw string) (*int64, bool) {
	s := strings.TrimSpace(raw)
	if s == "" {
		return nil, false
	}
	v, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return nil, false
	}
	return &v, true
}

func extractOriginalPath(r *http.Request) string {
	for _, k := range []string{"x-envoy-original-path", "x-original-path", "x-request-path", ":path"} {
		if v := strings.TrimSpace(r.Header.Get(k)); v != "" {
			return v
		}
	}
	if r.URL != nil {
		if r.URL.Path != "" {
			return r.URL.Path
		}
	}
	return r.URL.Path
}

func shouldArm(path string, h http.Header) bool {
	lowerPath := strings.ToLower(path)
	if strings.Contains(lowerPath, "/set_enabled") || strings.Contains(lowerPath, "/healthz") {
		return false
	}

	switch matchMode {
	case "all":
		return true
	case "header", "header_or_path", "header_and_path":
		got := h.Get(matchHeader)
		headerOK := got == matchHeaderValue
		pathOK := matchPathPrefix == "" || strings.HasPrefix(path, matchPathPrefix)
		switch matchMode {
		case "header":
			return headerOK
		case "header_or_path":
			return headerOK || pathOK
		case "header_and_path":
			return headerOK && pathOK
		}
	case "path":
		if matchPathPrefix == "" {
			return true
		}
		return strings.HasPrefix(path, matchPathPrefix)
	}
	return true
}

func maxInt64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

func (s *gateState) snapshotConfig() (active bool, delayNs, count, balance int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.armActive, s.armDelayNs, s.armCount, s.creditBalance
}

func (s *gateState) signalRefill() {
	if !refillEnabled {
		return
	}
	s.mu.Lock()
	s.st.RefillRequest++
	s.mu.Unlock()
	select {
	case refillCh <- struct{}{}:
	default:
	}
}

func armOnce(requestID, path string, delayNs, count int64) (bool, int, string, float64) {
	payload := map[string]any{
		"namespace": podNamespace,
		"pod":       podName,
		"container": containerName,
		"source":    sourceID,
		"delay_ns":  delayNs,
		"count":     count,
	}
	raw, _ := json.Marshal(payload)
	req, err := http.NewRequest(http.MethodPost, armURL, bytes.NewReader(raw))
	if err != nil {
		return false, 0, err.Error(), 0
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-MCOZ-Request-Id", requestID)
	req.Header.Set("X-MCOZ-Path", path)
	log.Printf(
		"[MCOZ-GATE-GO] state=CREDIT_SEND req_id=%s path=%s delay_ns=%d count=%d target=%s",
		requestID, path, delayNs, count, armURL,
	)

	t0 := time.Now()
	resp, err := armClient.Do(req)
	elapsedMs := float64(time.Since(t0).Microseconds()) / 1000.0
	if err != nil {
		log.Printf(
			"[MCOZ-GATE-GO] state=CREDIT_SEND_FAIL req_id=%s path=%s delay_ns=%d count=%d err=%s elapsed_ms=%.3f",
			requestID, path, delayNs, count, err.Error(), elapsedMs,
		)
		return false, 0, err.Error(), elapsedMs
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	body := string(b)
	ok := resp.StatusCode >= 200 && resp.StatusCode < 300
	log.Printf(
		"[MCOZ-GATE-GO] state=CREDIT_SENT req_id=%s path=%s delay_ns=%d count=%d status=%d ok=%t elapsed_ms=%.3f",
		requestID, path, delayNs, count, resp.StatusCode, ok, elapsedMs,
	)
	return ok, resp.StatusCode, body, elapsedMs
}

func (s *gateState) doRefill(tokensReq int64, reason string) (bool, int, string, float64) {
	if tokensReq <= 0 {
		return true, 0, "", 0
	}

	active, delayNs, count, _ := s.snapshotConfig()
	if !active || delayNs <= 0 || count <= 0 {
		return false, 0, "inactive-or-invalid-config", 0
	}

	credits := tokensReq * count
	reqID := fmt.Sprintf("refill-%d", time.Now().UnixMicro())
	ok, statusCode, body, elapsedMs := armOnce(reqID, "/refill/"+reason, delayNs, credits)

	s.mu.Lock()
	defer s.mu.Unlock()
	s.st.LastRefillMs = elapsedMs

	if ok {
		s.creditBalance += tokensReq
		s.st.CreditBalanceReq = s.creditBalance
		s.st.RefillOK++
		s.st.RefillTokensAdded += tokensReq
		s.st.RefillCreditsAdd += credits
		s.st.LastError = ""
		return true, statusCode, body, elapsedMs
	}

	s.st.RefillFail++
	if len(body) > 200 {
		body = body[:200]
	}
	if statusCode != 0 {
		s.st.LastError = fmt.Sprintf("refill failed status=%d body=%s", statusCode, body)
	} else {
		s.st.LastError = fmt.Sprintf("refill failed err=%s", body)
	}
	return false, statusCode, body, elapsedMs
}

func (s *gateState) currentRefillNeed() (int64, int64, int64, int64) {
	if !refillEnabled {
		_, delayNs, count, balance := s.snapshotConfig()
		return 0, delayNs, count, balance
	}
	active, delayNs, count, balance := s.snapshotConfig()
	if !active || delayNs <= 0 || count <= 0 {
		return 0, delayNs, count, balance
	}
	if balance >= refillLowWater {
		return 0, delayNs, count, balance
	}
	need := maxInt64(refillBatchReq, refillTargetReq-balance)
	if need <= 0 {
		need = refillBatchReq
	}
	return need, delayNs, count, balance
}

func (s *gateState) refillWorker(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-refillCh:
			for {
				need, _, _, _ := s.currentRefillNeed()
				if need <= 0 {
					break
				}
				ok, _, _, _ := s.doRefill(need, "async")
				if !ok {
					break
				}
			}
		}
	}
}

func (s *gateState) primeOnEnable() refillResult {
	if !refillEnabled {
		_, _, _, balance := s.snapshotConfig()
		return refillResult{
			Attempted: false,
			OK:        true,
			Reason:    "refill-disabled",
			Balance:   balance,
		}
	}
	active, delayNs, count, balance := s.snapshotConfig()
	if !active || delayNs <= 0 || count <= 0 {
		return refillResult{Attempted: false, OK: true, Reason: "inactive-or-invalid-config", Balance: balance}
	}
	need := refillTargetReq - balance
	if need <= 0 {
		return refillResult{Attempted: false, OK: true, Reason: "already-primed", Balance: balance}
	}
	ok, statusCode, _, elapsedMs := s.doRefill(need, "enable")
	_, _, _, nowBal := s.snapshotConfig()
	return refillResult{
		Attempted:      true,
		OK:             ok,
		StatusCode:     statusCode,
		ElapsedMs:      elapsedMs,
		RequestedToken: need,
		Balance:        nowBal,
	}
}

func (s *gateState) setArmConfig(delayNs *int64, count *int64) (changed bool, curDelay, curCount, balance int64) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if delayNs != nil && *delayNs >= 0 && *delayNs != s.armDelayNs {
		s.armDelayNs = *delayNs
		changed = true
	}
	if count != nil && *count >= 0 && *count != s.armCount {
		s.armCount = *count
		changed = true
	}
	if changed {
		s.creditBalance = 0
	}
	s.st.ArmDelayNs = s.armDelayNs
	s.st.ArmCount = s.armCount
	s.st.CreditBalanceReq = s.creditBalance
	return changed, s.armDelayNs, s.armCount, s.creditBalance
}

func (s *gateState) setArmActive(active bool) (changed bool) {
	s.mu.Lock()
	if s.armActive != active {
		changed = true
		s.st.ArmToggleCount++
	}
	s.armActive = active
	if active {
		s.st.ArmActive = 1
	} else {
		s.st.ArmActive = 0
		s.creditBalance = 0
		s.st.CreditBalanceReq = 0
	}
	s.mu.Unlock()

	if active {
		s.signalRefill()
	}
	return changed
}

func (s *gateState) consumeToken() (bool, int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.creditBalance > 0 {
		s.creditBalance--
		s.st.CreditBalanceReq = s.creditBalance
		return true, s.creditBalance
	}
	s.st.CreditMiss++
	return false, s.creditBalance
}

func (s *gateState) snapshotStats() (stats, int64, int64, bool, int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.st, s.armDelayNs, s.armCount, s.armActive, s.creditBalance
}

func (s *gateState) handleSetEnabled(w http.ResponseWriter, r *http.Request, body []byte) {
	query := r.URL.Query()

	var enabledPtr *bool
	if vals, ok := query["enabled"]; ok && len(vals) > 0 {
		if v, ok := parseBoolPtr(vals[len(vals)-1]); ok {
			enabledPtr = v
		}
	}
	if enabledPtr == nil {
		if vals, ok := query["active"]; ok && len(vals) > 0 {
			if v, ok := parseBoolPtr(vals[len(vals)-1]); ok {
				enabledPtr = v
			}
		}
	}

	var delayPtr *int64
	if vals, ok := query["delay_ns"]; ok && len(vals) > 0 {
		if v, ok := parseInt64Ptr(vals[len(vals)-1]); ok {
			delayPtr = v
		}
	}
	if delayPtr == nil {
		if vals, ok := query["delayNs"]; ok && len(vals) > 0 {
			if v, ok := parseInt64Ptr(vals[len(vals)-1]); ok {
				delayPtr = v
			}
		}
	}

	var countPtr *int64
	if vals, ok := query["count"]; ok && len(vals) > 0 {
		if v, ok := parseInt64Ptr(vals[len(vals)-1]); ok {
			countPtr = v
		}
	}

	ctype := strings.ToLower(r.Header.Get("Content-Type"))
	if len(body) > 0 {
		if strings.Contains(ctype, "application/json") {
			var obj map[string]any
			if err := json.Unmarshal(body, &obj); err == nil {
				if enabledPtr == nil {
					if raw, ok := obj["enabled"]; ok {
						if v, ok := parseBoolPtr(fmt.Sprintf("%v", raw)); ok {
							enabledPtr = v
						}
					} else if raw, ok := obj["active"]; ok {
						if v, ok := parseBoolPtr(fmt.Sprintf("%v", raw)); ok {
							enabledPtr = v
						}
					}
				}
				if delayPtr == nil {
					if raw, ok := obj["delay_ns"]; ok {
						if v, ok := parseInt64Ptr(fmt.Sprintf("%v", raw)); ok {
							delayPtr = v
						}
					} else if raw, ok := obj["delayNs"]; ok {
						if v, ok := parseInt64Ptr(fmt.Sprintf("%v", raw)); ok {
							delayPtr = v
						}
					}
				}
				if countPtr == nil {
					if raw, ok := obj["count"]; ok {
						if v, ok := parseInt64Ptr(fmt.Sprintf("%v", raw)); ok {
							countPtr = v
						}
					}
				}
			}
		} else {
			if vals, err := url.ParseQuery(string(body)); err == nil {
				if enabledPtr == nil {
					if v := vals.Get("enabled"); v != "" {
						if b, ok := parseBoolPtr(v); ok {
							enabledPtr = b
						}
					} else if v := vals.Get("active"); v != "" {
						if b, ok := parseBoolPtr(v); ok {
							enabledPtr = b
						}
					}
				}
				if delayPtr == nil {
					if v := vals.Get("delay_ns"); v != "" {
						if i, ok := parseInt64Ptr(v); ok {
							delayPtr = i
						}
					} else if v := vals.Get("delayNs"); v != "" {
						if i, ok := parseInt64Ptr(v); ok {
							delayPtr = i
						}
					}
				}
				if countPtr == nil {
					if v := vals.Get("count"); v != "" {
						if i, ok := parseInt64Ptr(v); ok {
							countPtr = i
						}
					}
				}
			}
		}
	}

	if enabledPtr == nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"ok":      false,
			"error":   "missing enabled",
			"example": "POST /set_enabled?enabled=1",
		})
		return
	}

	cfgChanged, curDelay, curCount, balance := s.setArmConfig(delayPtr, countPtr)
	changed := s.setArmActive(*enabledPtr)

	var prime *refillResult
	if *enabledPtr && (cfgChanged || changed) {
		p := s.primeOnEnable()
		prime = &p
		s.signalRefill()
		_, _, _, _, balance = s.snapshotStats()
	}

	payload := map[string]any{
		"ok":                 true,
		"enabled":            *enabledPtr,
		"changed":            changed,
		"config_changed":     cfgChanged,
		"delay_ns":           curDelay,
		"count":              curCount,
		"credit_balance_req": balance,
		"prime":              prime,
		"service":            "mcoz-gate-go",
		"pod":                podName,
		"namespace":          podNamespace,
	}
	writeJSON(w, http.StatusOK, payload)
}

func (s *gateState) handleHealthz(w http.ResponseWriter, _ *http.Request) {
	_, _, _, armActive, _ := s.snapshotStats()
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":         true,
		"service":    "mcoz-gate-go",
		"pod":        podName,
		"namespace":  podNamespace,
		"arm_active": armActive,
	})
}

func (s *gateState) handleMetrics(w http.ResponseWriter, _ *http.Request) {
	st, delayNs, count, armActive, _ := s.snapshotStats()
	armActiveInt := 0
	if armActive {
		armActiveInt = 1
	}
	payload := map[string]any{
		"triggered":              st.Triggered,
		"armed_ok":               st.ArmedOK,
		"armed_fail":             st.ArmedFail,
		"arm_suppressed":         st.ArmSuppressed,
		"arm_suspended_count":    st.ArmSuspendedCount,
		"arm_suspend_until_unix": st.ArmSuspendUntil,
		"arm_suspend_reason":     st.ArmSuspendReason,
		"arm_active":             armActiveInt,
		"arm_toggle_count":       st.ArmToggleCount,
		"disabled_skip":          st.DisabledSkip,
		"skipped":                st.Skipped,
		"arm_delay_ns":           st.ArmDelayNs,
		"arm_count":              st.ArmCount,
		"credit_balance_req":     st.CreditBalanceReq,
		"credit_miss":            st.CreditMiss,
		"refill_request":         st.RefillRequest,
		"refill_ok":              st.RefillOK,
		"refill_fail":            st.RefillFail,
		"refill_tokens_added":    st.RefillTokensAdded,
		"refill_credits_added":   st.RefillCreditsAdd,
		"last_refill_ms":         st.LastRefillMs,
		"config_skip":            st.ConfigSkip,
		"last_error":             st.LastError,
		"last_path":              st.LastPath,
		"last_request_id":        st.LastRequestID,
		"mode":                   "ext_authz-gate-go",
		"arm_url":                armURL,
		"delay_ns":               delayNs,
		"count":                  count,
		"container":              containerName,
		"match_mode":             matchMode,
		"match_header":           matchHeader,
		"match_header_value":     matchHeaderValue,
		"match_path_prefix":      matchPathPrefix,
		"arm_active_default":     armActiveDefault,
		"refill_target_req":      refillTargetReq,
		"refill_low_water_req":   refillLowWater,
		"refill_batch_req":       refillBatchReq,
		"refill_enabled":         refillEnabled,
		"send_on_zero_delay":     sendOnZeroDelay,
		"sync_refill_on_miss":    syncRefillMiss,
		"verbose_events":         verboseEvents,
	}
	writeJSON(w, http.StatusOK, payload)
}

func (s *gateState) handleCheck(w http.ResponseWriter, r *http.Request) {
	started := time.Now()
	path := extractOriginalPath(r)
	requestID := r.Header.Get("x-request-id")
	if strings.TrimSpace(requestID) == "" {
		requestID = fmt.Sprintf("no-id-%d", time.Now().UnixMicro())
	}

	s.mu.Lock()
	s.st.Triggered++
	s.st.LastPath = path
	s.st.LastRequestID = requestID
	s.mu.Unlock()

	armDisabled := false
	armed := false
	errMsg := ""

	active, delayNs, count, _ := s.snapshotConfig()

	if !active {
		armDisabled = true
		s.mu.Lock()
		s.st.DisabledSkip++
		s.mu.Unlock()
	} else if shouldArm(path, r.Header) {
		if count <= 0 {
			s.mu.Lock()
			s.st.ConfigSkip++
			s.mu.Unlock()
		} else if delayNs <= 0 {
			if sendOnZeroDelay {
				// Keep ext_authz->mcoz arm RPC overhead even when virtual delay is disabled.
				// count=0 means no credit is added, so no injection can be consumed.
				ok, statusCode, body, _ := armOnce(requestID, path, 0, 0)
				if !ok {
					errMsg = fmt.Sprintf("zero-delay arm failed status=%d body=%s", statusCode, truncate(body, 120))
					s.mu.Lock()
					s.st.ArmedFail++
					s.st.LastError = errMsg
					s.mu.Unlock()
				}
			} else {
				s.mu.Lock()
				s.st.ConfigSkip++
				s.mu.Unlock()
			}
		} else {
			// Refill-disabled mode: arm once per matched request directly.
			if !refillEnabled {
				ok, statusCode, body, _ := armOnce(requestID, path, delayNs, count)
				if ok {
					armed = true
					s.mu.Lock()
					s.st.ArmedOK++
					s.mu.Unlock()
				} else {
					errMsg = fmt.Sprintf("direct arm failed status=%d body=%s", statusCode, truncate(body, 120))
					s.mu.Lock()
					s.st.ArmedFail++
					s.st.LastError = errMsg
					s.mu.Unlock()
				}
			} else {
				consumed, bal := s.consumeToken()
				if consumed {
					armed = true
					s.mu.Lock()
					s.st.ArmedOK++
					s.mu.Unlock()
					if bal <= refillLowWater {
						s.signalRefill()
					}
					if verboseEvents {
						log.Printf("[MCOZ-GATE-GO] state=ARMED req_id=%s path=%s balance=%d", requestID, path, bal)
					}
				} else {
					s.signalRefill()
					if syncRefillMiss {
						ok, statusCode, body, _ := s.doRefill(maxInt64(1, refillBatchReq), "sync-miss")
						if ok {
							consumed2, bal2 := s.consumeToken()
							if consumed2 {
								armed = true
								s.mu.Lock()
								s.st.ArmedOK++
								s.mu.Unlock()
								if bal2 <= refillLowWater {
									s.signalRefill()
								}
							} else {
								errMsg = "credit miss after successful sync refill"
								s.mu.Lock()
								s.st.ArmedFail++
								s.st.LastError = errMsg
								s.mu.Unlock()
							}
						} else {
							errMsg = fmt.Sprintf("sync refill failed status=%d body=%s", statusCode, truncate(body, 120))
							s.mu.Lock()
							s.st.ArmedFail++
							s.st.LastError = errMsg
							s.mu.Unlock()
						}
					} else {
						errMsg = "credit miss; refill queued"
						s.mu.Lock()
						s.st.ArmedFail++
						s.st.LastError = errMsg
						s.mu.Unlock()
					}
				}
			}
		}
	} else {
		s.mu.Lock()
		s.st.Skipped++
		s.mu.Unlock()
	}

	elapsedUs := time.Since(started).Microseconds()

	headers := map[string]string{}
	if debugHeaders {
		_, curDelay, curCount, activeNow, _ := s.snapshotStats()
		headers["x-mcoz-triggered"] = "1"
		if armed {
			headers["x-mcoz-armed"] = "1"
		} else {
			headers["x-mcoz-armed"] = "0"
		}
		headers["x-mcoz-delay-ns"] = strconv.FormatInt(curDelay, 10)
		headers["x-mcoz-count"] = strconv.FormatInt(curCount, 10)
		headers["x-mcoz-gate-us"] = strconv.FormatInt(elapsedUs, 10)
		headers["x-mcoz-arm-suspended"] = "0"
		if activeNow {
			headers["x-mcoz-arm-active"] = "1"
		} else {
			headers["x-mcoz-arm-active"] = "0"
		}
		if armDisabled {
			headers["x-mcoz-arm-disabled"] = "1"
		} else {
			headers["x-mcoz-arm-disabled"] = "0"
		}
		if errMsg != "" {
			headers["x-mcoz-arm-error"] = truncate(errMsg, 120)
		}
	}

	// ext_authz contract: HTTP 200 => allow
	writeText(w, http.StatusOK, "OK", headers)
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func (s *gateState) route(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path

	if r.Method == http.MethodGet && strings.HasPrefix(path, "/healthz") {
		s.handleHealthz(w, r)
		return
	}
	if r.Method == http.MethodGet && strings.HasPrefix(path, "/metrics") {
		s.handleMetrics(w, r)
		return
	}

	if strings.HasPrefix(path, "/set_enabled") {
		var body []byte
		if r.Body != nil {
			raw, _ := io.ReadAll(io.LimitReader(r.Body, 64*1024))
			body = raw
		}
		s.handleSetEnabled(w, r, body)
		return
	}

	s.handleCheck(w, r)
}

func normalizeRefillParams() {
	if refillTargetReq <= 0 {
		refillTargetReq = 16
	}
	if refillLowWater < 0 {
		refillLowWater = 0
	}
	if refillLowWater >= refillTargetReq {
		refillLowWater = maxInt64(0, refillTargetReq/2)
	}
	if refillBatchReq <= 0 {
		refillBatchReq = 8
	}
}

func main() {
	normalizeRefillParams()

	g.armActive = armActiveDefault
	g.armDelayNs = defaultDelayNs
	g.armCount = defaultCount
	g.creditBalance = 0

	g.st.ArmDelayNs = defaultDelayNs
	g.st.ArmCount = defaultCount
	g.st.CreditBalanceReq = 0
	if armActiveDefault {
		g.st.ArmActive = 1
	} else {
		g.st.ArmActive = 0
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go g.refillWorker(ctx)
	if armActiveDefault {
		g.signalRefill()
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/", g.route)

	srv := &http.Server{
		Addr:              fmt.Sprintf("0.0.0.0:%d", port),
		Handler:           mux,
		ReadHeaderTimeout: 2 * time.Second,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      5 * time.Second,
		IdleTimeout:       30 * time.Second,
	}

	log.Printf("[mcoz-gate-go] listening on 0.0.0.0:%d (pod=%s ns=%s arm=%s)", port, podName, podNamespace, armURL)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server failed: %v", err)
	}
}
