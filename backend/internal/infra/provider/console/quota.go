package console

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/account"
	egressdomain "github.com/chenyme/grok2api/backend/internal/domain/egress"
	infraegress "github.com/chenyme/grok2api/backend/internal/infra/egress"
	"github.com/chenyme/grok2api/backend/internal/infra/provider"
)

const (
	consoleQuotaTimeout                = 30 * time.Second
	consolePredictedChatRecoveryWindow = 24 * time.Hour
	consoleBotFlagDetectTimeout        = 10 * time.Second
	consoleBotFlagHomeURL              = "https://grok.com/"
	consoleBotFlagBodyLimit            = 2 << 20
)

// botFlagSourcePatterns 复用 GrokIQ 的 SSO 拆解口径：从 grok.com 响应
// （RSC JSON / HTML 内嵌 JSON）中提取 botFlagSource。
var (
	botFlagSourceJSONPattern = regexp.MustCompile(`"botFlagSource"\s*:\s*(-?\d+)`)
	botFlagSourceNullPattern = regexp.MustCompile(`"botFlagSource"\s*:\s*null`)
	lastAbsentLogMu          sync.Mutex
	lastAbsentLogAt          time.Time
)

func (a *Adapter) SyncQuota(ctx context.Context, credential account.Credential) (provider.QuotaSnapshot, error) {
	windows, syncedAt, err := a.syncConsoleQuotas(ctx, credential)
	if err != nil {
		return provider.QuotaSnapshot{}, err
	}
	return provider.QuotaSnapshot{Windows: windows, SyncedAt: syncedAt}, nil
}

func (a *Adapter) SyncQuotaMode(ctx context.Context, credential account.Credential, mode string) (account.QuotaWindow, error) {
	if !isConsoleQuotaMode(mode) {
		return account.QuotaWindow{}, fmt.Errorf("不支持的 Console 额度模式 %q", mode)
	}
	windows, _, err := a.syncConsoleQuotas(ctx, credential)
	if err != nil {
		return account.QuotaWindow{}, err
	}
	for _, window := range windows {
		if window.Mode == mode {
			return window, nil
		}
	}
	return account.QuotaWindow{}, fmt.Errorf("Console usage 响应缺少 %s 额度", consoleQuotaKind(mode))
}

func (a *Adapter) syncConsoleQuotas(ctx context.Context, credential account.Credential) ([]account.QuotaWindow, time.Time, error) {
	ssoToken, err := a.cipher.Decrypt(credential.EncryptedAccessToken)
	if err != nil {
		return nil, time.Time{}, err
	}
	requestCtx, cancel := context.WithTimeout(ctx, consoleQuotaTimeout)
	defer cancel()
	lease, err := a.egress.AcquireCredential(requestCtx, egressdomain.ScopeConsole, credential)
	if err != nil {
		return nil, time.Time{}, err
	}
	defer lease.Release()
	endpoint := consoleV1Endpoint(a.config().BaseURL, "/usage")
	response, err := a.doDPoPRequest(requestCtx, credential, ssoToken, lease, http.MethodGet, endpoint, nil, "application/json")
	if err != nil {
		a.egress.FeedbackForScope(context.WithoutCancel(ctx), egressdomain.ScopeConsole, lease.NodeID, 0, err)
		return nil, time.Time{}, err
	}
	data, truncated, readErr := provider.ReadDiagnosticBody(response.Body)
	_ = response.Body.Close()
	if readErr != nil {
		return nil, time.Time{}, readErr
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		if response.StatusCode == http.StatusUnauthorized ||
			(response.StatusCode == http.StatusForbidden && provider.IsDefinitiveAccountBlockBody(data)) {
			return nil, time.Time{}, fmt.Errorf("%w: Console usage rejected", provider.ErrUnauthorized)
		}
		dpopRequired := response.StatusCode == http.StatusForbidden && provider.IsDPoPProofRequiredBody(data)
		if response.StatusCode == http.StatusForbidden && shouldInvalidateConsoleClearance(data) {
			lease.InvalidateClearance()
		}
		if !dpopRequired {
			a.egress.FeedbackForScope(context.WithoutCancel(ctx), egressdomain.ScopeConsole, lease.NodeID, response.StatusCode, nil)
		}
		suffix := ""
		if truncated {
			suffix = " (响应已截断)"
		}
		return nil, time.Time{}, fmt.Errorf("Console usage 接口返回 %d%s", response.StatusCode, suffix)
	}
	a.egress.FeedbackForScope(context.WithoutCancel(ctx), egressdomain.ScopeConsole, lease.NodeID, response.StatusCode, nil)
	var payload struct {
		Quotas []struct {
			Kind           string `json:"kind"`
			Limit          int    `json:"limit"`
			Used           int    `json:"used"`
			Remaining      int    `json:"remaining"`
			LastConsumedAt int64  `json:"last_consumed_at"`
		} `json:"quotas"`
	}
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, time.Time{}, fmt.Errorf("解析 Console usage: %w", err)
	}
	now := time.Now().UTC()
	byMode := make(map[string]account.QuotaWindow, 3)
	for _, quota := range payload.Quotas {
		mode := consoleQuotaMode(quota.Kind)
		if mode == "" {
			continue
		}
		if quota.Limit < 0 || quota.Used < 0 || quota.Remaining < 0 || quota.Remaining > quota.Limit {
			return nil, time.Time{}, fmt.Errorf("Console %s 额度响应无效", consoleQuotaKind(mode))
		}
		usagePercent := 0.0
		if quota.Limit > 0 {
			usagePercent = float64(quota.Limit-quota.Remaining) / float64(quota.Limit) * 100
		}
		windowSeconds := 0
		var resetAt *time.Time
		if mode == QuotaMode {
			windowSeconds = int(consolePredictedChatRecoveryWindow / time.Second)
			if quota.Remaining == 0 {
				predicted := now.Add(consolePredictedChatRecoveryWindow)
				resetAt = &predicted
			}
		}
		byMode[mode] = account.QuotaWindow{
			AccountID: credential.ID, Mode: mode, Remaining: quota.Remaining, Total: quota.Limit,
			UsagePercent: usagePercent, WindowSeconds: windowSeconds, ResetAt: resetAt,
			SyncedAt: &now, Source: account.QuotaSourceUpstream, UpdatedAt: now,
		}
	}
	for _, mode := range []string{QuotaMode, QuotaModeImage, QuotaModeVideo} {
		if _, ok := byMode[mode]; !ok {
			return nil, time.Time{}, fmt.Errorf("Console usage 响应缺少 %s 额度", consoleQuotaKind(mode))
		}
	}
	windows := make([]account.QuotaWindow, 0, 3)
	for _, mode := range []string{QuotaMode, QuotaModeImage, QuotaModeVideo} {
		if window, ok := byMode[mode]; ok {
			windows = append(windows, window)
		}
	}
	// 风控联动检测：挂点 A 优先解析 /usage 响应自带的字段；
	// 无风控字段时回退挂点 B（复用本次 SSO 会话 + egress 出口打 grok.com 拆解）。
	// 无论结果如何都上报（含安全结果 0），用于记录最近探测时间与状态。
	source := parseConsoleUsageRisk(data)
	origin := "usage"
	if source == 0 {
		source = a.detectConsoleBotFlag(context.WithoutCancel(ctx), credential, ssoToken, lease)
		origin = "grok-home"
	}
	a.reportBotFlag(context.WithoutCancel(ctx), credential, source, origin)
	return windows, now, nil
}

// parseConsoleUsageRisk 挂点 A：从 /usage 响应体解析风控字段。
// 返回 1/2 表示风控；0 表示响应未携带风控字段。
func parseConsoleUsageRisk(data []byte) int {
	if len(data) == 0 {
		return 0
	}
	var payload map[string]any
	if err := json.Unmarshal(data, &payload); err != nil {
		return 0
	}
	for _, key := range []string{"botFlagSource", "bot_flag_source", "bfs", "botFlag", "risk"} {
		raw, ok := payload[key]
		if !ok || raw == nil {
			continue
		}
		switch value := raw.(type) {
		case float64:
			if value == 1 || value == 2 {
				slog.Info("console_usage_bot_flag_detected", "field", key, "value", int(value))
				return int(value)
			}
		case string:
			if value == "1" || value == "2" {
				slog.Info("console_usage_bot_flag_detected", "field", key, "value", value)
				n := 0
				_, _ = fmt.Sscanf(value, "%d", &n)
				if n == 1 || n == 2 {
					return n
				}
			}
		}
	}
	if len(payload) > 0 {
		keys := make([]string, 0, len(payload))
		for key := range payload {
			keys = append(keys, key)
		}
		// 挂点 A 未命中属正常路径；按 10 分钟节流打 Info，便于确认检测链路生效。
		lastAbsentLogMu.Lock()
		due := time.Since(lastAbsentLogAt) >= 10*time.Minute
		if due {
			lastAbsentLogAt = time.Now()
		}
		lastAbsentLogMu.Unlock()
		if due {
			slog.Info("console_usage_risk_fields_absent", "top_level_keys", strings.Join(keys, ","))
		}
	}
	return 0
}

// detectConsoleBotFlag 挂点 B：复用本次 SSO 会话与 egress 出口，
// 访问 grok.com 首页并按 GrokIQ 口径拆解 botFlagSource（1/2 为风控）。
func (a *Adapter) detectConsoleBotFlag(ctx context.Context, credential account.Credential, ssoToken string, lease *infraegress.Lease) int {
	if ssoToken == "" || lease == nil {
		return 0
	}
	requestCtx, cancel := context.WithTimeout(ctx, consoleBotFlagDetectTimeout)
	defer cancel()
	request, err := http.NewRequestWithContext(requestCtx, http.MethodGet, consoleBotFlagHomeURL, nil)
	if err != nil {
		return 0
	}
	request.Header.Set("Cookie", infraegress.BuildSSOCookie(ssoToken, ""))
	request.Header.Set("Accept", "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8")
	request.Header.Set("Accept-Language", "en-US,en;q=0.9")
	response, err := lease.Do(request)
	if err != nil {
		slog.Debug("console_bot_flag_detect_request_failed", "account_id", credential.ID, "error", err)
		return 0
	}
	body, readErr := io.ReadAll(io.LimitReader(response.Body, consoleBotFlagBodyLimit+1))
	_ = response.Body.Close()
	if readErr != nil || len(body) > consoleBotFlagBodyLimit {
		slog.Debug("console_bot_flag_detect_body_read_failed", "account_id", credential.ID, "error", readErr)
		return 0
	}
	if response.StatusCode != http.StatusOK {
		slog.Debug("console_bot_flag_detect_http_status", "account_id", credential.ID, "status", response.StatusCode)
		return 0
	}
	return parseBotFlagSourceFromBody(body)
}

// parseBotFlagSourceFromBody 从 grok.com 响应中提取 botFlagSource（GrokIQ 同款正则）。
func parseBotFlagSourceFromBody(body []byte) int {
	normalized := strings.Map(func(r rune) rune {
		switch r {
		case ' ', '\n', '\r', '\t':
			return -1
		default:
			return r
		}
	}, string(body))
	if botFlagSourceNullPattern.MatchString(normalized) {
		return 0
	}
	match := botFlagSourceJSONPattern.FindStringSubmatch(normalized)
	if len(match) < 2 {
		return 0
	}
	var source int
	if _, err := fmt.Sscanf(match[1], "%d", &source); err != nil {
		return 0
	}
	if source != 1 && source != 2 {
		return 0
	}
	slog.Info("console_bot_flag_detected_from_grok_home", "botFlagSource", source)
	return source
}

// reportBotFlag 将检测结果上报给账号服务（落库 + 三渠道传播），失败不影响额度同步。
func (a *Adapter) reportBotFlag(ctx context.Context, credential account.Credential, source int, origin string) {
	a.mu.RLock()
	reporter := a.botFlagReporter
	a.mu.RUnlock()
	if reporter == nil {
		slog.Debug("console_bot_flag_reporter_unset", "origin", origin)
		return
	}
	reporter(ctx, credential, source)
}

func isConsoleQuotaMode(mode string) bool {
	return mode == QuotaMode || mode == QuotaModeImage || mode == QuotaModeVideo
}

func consoleQuotaMode(kind string) string {
	switch strings.ToLower(strings.TrimSpace(kind)) {
	case "chat":
		return QuotaMode
	case "image":
		return QuotaModeImage
	case "video":
		return QuotaModeVideo
	default:
		return ""
	}
}

func consoleQuotaKind(mode string) string {
	switch mode {
	case QuotaMode:
		return "chat"
	case QuotaModeImage:
		return "image"
	case QuotaModeVideo:
		return "video"
	default:
		return mode
	}
}
