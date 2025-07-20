package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

type KeyManagerConfig struct {
	PriorityKeys           []string                 `json:"priority_keys"`
	SecondaryKeys          []string                 `json:"secondary_keys"`
	Models                 map[string]LanguageModel `json:"models"`
	ResetAfter             string                   `json:"reset_after"` // Format: "00:00" (HH:MM)
	NextQuotaResetDatetime string                   `json:"next_quota_reset_datetime"`
	Timezone               string                   `json:"timezone"` // e.g., "America/Los_Angeles"
	DefaultModel           string                   `json:"default_model"`
}

type LanguageModel struct {
	ModelName string `json:"-"`
	TpmLimit  int    `json:"tpm_limit"`
	TpdLimit  *int   `json:"tpd_limit"`
}

type UsageData struct {
	Timestamp int `json:"timestamp"`
	CostToken int `json:"cost_token"`
}

type LanguageModelUsage struct {
	LanguageModel
	TotalTokenUse         int         `json:"total_tokens"`
	TodayUsage            int         `json:"today_usage,omitempty"`
	Past24HoursTokenUsage []UsageData `json:"past_24hrs_usage_data"`
	ProbablyExceeded      bool        `json:"probably_exceeded"`
	Exceeded              bool        `json:"exceeded"`
	// Fields calculated at runtime
	JustHit429        bool        `json:"-"`
	Past60sTokenUsage []UsageData `json:"-"`
}

type KeyInfo struct {
	Key          string
	IsPriority   bool
	CurrentIndex int
}

type KeyManager struct {
	config    *KeyManagerConfig
	keys      []KeyInfo
	usage     map[string]*LanguageModelUsage // key: modelName_key
	mutex     sync.Mutex
	lastSaved time.Time
	ticker    *time.Ticker
	stopChan  chan struct{}
	nextReset time.Time

	// For status page
	lastHourTokenUsage map[string][]UsageData // key: modelName, value: usage data
	lastHourKeyUsage   map[string][]UsageData // key: apiKey, value: usage data
	usageHistoryMutex  sync.Mutex
}

// Status page data structures
type StatusData struct {
	GrandTotalTokens        int                    `json:"grand_total_tokens"`
	GrandTotalTodayUsage    int                    `json:"grand_total_today_usage"`
	CurrentMaskedKey        string                 `json:"current_masked_key"`
	CurrentRawKey           string                 `json:"-"` // Internal use, not marshalled
	KeyUsageStatus          map[string]KeyStatus   `json:"key_usage_status"`
	PriorityKeys            []string               `json:"priority_keys"`
	SecondaryKeys           []string               `json:"secondary_keys"`
	UnavailableKeys         []string               `json:"unavailable_keys"`
	RateLimitedKeys         []string               `json:"rate_limited_keys"`
	QuotaExhaustedKeys      []string               `json:"quota_exhausted_keys"`
	ModelOrder              []string               `json:"model_order"`
	ModelsConfig            map[string]ModelConfig `json:"models_config"`
	ModelChartData          ChartData              `json:"model_chart_data"`
	KeyChartData            ChartData              `json:"key_chart_data"`
	ActiveKeyModelChartData ChartData              `json:"active_key_model_chart_data"`
}

type KeyStatus map[string]ModelUsageStatus // key: modelName

type ModelUsageStatus struct {
	TokensLastMinute      int  `json:"tokens_last_minute"`
	TotalTokens           int  `json:"total_tokens"`
	TodayUsage            int  `json:"today_usage"`
	IsTemporarilyDisabled bool `json:"is_temporarily_disabled"`
	DailyQuotaExceeded    bool `json:"daily_quota_exceeded"`
}

type ModelConfig struct {
	TpmLimit int `json:"tpm_limit"`
}

type ChartData struct {
	Labels   []string       `json:"labels"`
	Datasets []ChartDataset `json:"datasets"`
}

type ChartDataset struct {
	Label           string  `json:"label"`
	Data            []int   `json:"data"`
	Fill            bool    `json:"fill"`
	BorderColor     string  `json:"borderColor"`
	BackgroundColor string  `json:"backgroundColor"`
	Tension         float64 `json:"tension"`
}

func NewKeyManager() (*KeyManager, error) {
	config, err := LoadConfig()
	if err != nil {
		return nil, err
	}

	usage, err := LoadKeyUsage(config)
	if err != nil {
		return nil, err
	}

	var keys []KeyInfo
	for i, key := range config.PriorityKeys {
		keys = append(keys, KeyInfo{Key: key, IsPriority: true, CurrentIndex: i})
	}
	for i, key := range config.SecondaryKeys {
		keys = append(keys, KeyInfo{Key: key, IsPriority: false, CurrentIndex: len(config.PriorityKeys) + i})
	}

	loc, err := time.LoadLocation(config.Timezone)
	if err != nil {
		return nil, fmt.Errorf("invalid timezone: %v", err)
	}
	nextReset, err := time.ParseInLocation("2006-01-02 15:04", config.NextQuotaResetDatetime, loc)
	if err != nil {
		return nil, fmt.Errorf("invalid next_quota_reset_datetime: %v", err)
	}

	km := &KeyManager{
		config:             config,
		keys:               keys,
		usage:              usage,
		lastSaved:          time.Now(),
		ticker:             time.NewTicker(1 * time.Minute),
		stopChan:           make(chan struct{}),
		nextReset:          nextReset,
		lastHourTokenUsage: make(map[string][]UsageData),
		lastHourKeyUsage:   make(map[string][]UsageData),
	}

	go km.autoSave()
	go km.usageHistoryTracker()
	go km.resetScheduler()

	return km, nil
}

func (km *KeyManager) Stop() {
	km.ticker.Stop()
	close(km.stopChan)
	km.SaveUsage()
}

func (km *KeyManager) autoSave() {
	for {
		select {
		case <-km.ticker.C:
			km.SaveUsage()
		case <-km.stopChan:
			return
		}
	}
}

func (km *KeyManager) usageHistoryTracker() {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			km.recordUsageHistory()
		case <-km.stopChan:
			return
		}
	}
}

func (km *KeyManager) recordUsageHistory() {
	km.mutex.Lock()
	defer km.mutex.Unlock()
	km.usageHistoryMutex.Lock()
	defer km.usageHistoryMutex.Unlock()

	now := time.Now().Unix()
	totalTokensPerModel := make(map[string]int)
	totalTokensPerKey := make(map[string]int)

	allKeys := append(km.config.PriorityKeys, km.config.SecondaryKeys...)
	keyExists := make(map[string]bool)
	for _, k := range allKeys {
		keyExists[k] = true
	}

	for usageKey, usage := range km.usage {
		modelName := usage.LanguageModel.ModelName
		// Extract key from usageKey (format: modelName_key)
		key := strings.TrimPrefix(usageKey, modelName+"_")
		if !keyExists[key] {
			continue // Skip usage data for keys no longer in config
		}

		UpdateLanguageModelUsage(usage, now)
		var tokensLastMinute int
		for _, data := range usage.Past60sTokenUsage {
			tokensLastMinute += data.CostToken
		}
		totalTokensPerModel[modelName] += tokensLastMinute
		totalTokensPerKey[key] += tokensLastMinute
	}

	// Update model usage history
	for modelName, totalTokens := range totalTokensPerModel {
		newData := UsageData{Timestamp: int(now), CostToken: totalTokens}
		history := km.lastHourTokenUsage[modelName]
		history = append(history, newData)
		// Keep only the last hour
		var updatedHistory []UsageData
		for _, data := range history {
			if int64(data.Timestamp) >= now-3600 {
				updatedHistory = append(updatedHistory, data)
			}
		}
		km.lastHourTokenUsage[modelName] = updatedHistory
	}

	// Update key usage history
	for key, totalTokens := range totalTokensPerKey {
		newData := UsageData{Timestamp: int(now), CostToken: totalTokens}
		history := km.lastHourKeyUsage[key]
		history = append(history, newData)
		// Keep only the last hour
		var updatedHistory []UsageData
		for _, data := range history {
			if int64(data.Timestamp) >= now-3600 {
				updatedHistory = append(updatedHistory, data)
			}
		}
		km.lastHourKeyUsage[key] = updatedHistory
	}
}

func (km *KeyManager) resetScheduler() {
	for {
		now := time.Now()
		if now.After(km.nextReset) {
			km.resetQuotas()
			// Calculate next reset time
			resetTime, _ := time.Parse("15:04", km.config.ResetAfter)
			today := time.Now().In(km.nextReset.Location())
			next := time.Date(today.Year(), today.Month(), today.Day(), resetTime.Hour(), resetTime.Minute(), 0, 0, km.nextReset.Location())
			if next.Before(today) {
				next = next.AddDate(0, 0, 1)
			}
			km.nextReset = next
			km.config.NextQuotaResetDatetime = km.nextReset.Format("2006-01-02 15:04")
			if err := saveConfig(km.config); err != nil {
				log.Printf("ERROR: failed to save config after quota reset: %v", err)
			}
			log.Printf("Quotas reset. Next reset scheduled for: %s", km.nextReset.Format("2006-01-02 15:04:05"))
		}
		// Sleep until the next check
		time.Sleep(1 * time.Minute)
	}
}

func (km *KeyManager) resetQuotas() {
	km.mutex.Lock()
	defer km.mutex.Unlock()

	for _, usage := range km.usage {
		// usage.TotalTokenUse is a lifetime cumulative value.
		// We only reset the daily counters.
		usage.TodayUsage = 0
		usage.Past24HoursTokenUsage = []UsageData{}
		usage.Exceeded = false
		usage.ProbablyExceeded = false
	}
	log.Println("All daily quotas have been reset.")
}

func (km *KeyManager) GetKey(modelName string) (string, string, time.Duration, error) {
	km.mutex.Lock()
	defer km.mutex.Unlock()

	originalModelName := modelName
	if _, ok := km.config.Models[modelName]; !ok {
		modelName = km.config.DefaultModel
		log.Printf("Model '%s' not found, falling back to default model '%s'", originalModelName, modelName)
	}
	model := km.config.Models[modelName]

	now := time.Now().Unix()

	var availableKeys []KeyInfo
	var probablyAvailableKeys []KeyInfo

	for _, keyInfo := range km.keys {
		usageKey := modelName + "_" + keyInfo.Key
		usage, ok := km.usage[usageKey]
		if !ok {
			log.Printf("Usage key '%s' not found, skipping key %s", usageKey, keyInfo.Key[:4])
			continue
		}

		UpdateLanguageModelUsage(usage, now)

		// Check for daily usage limit of 4.1M tokens
		if usage.TodayUsage >= 4100000 {
			usage.Exceeded = true
			log.Printf("Key %s for model %s reached daily usage limit of 4.1M tokens. Marked as 'exceeded'.", keyInfo.Key[:4], modelName)
			continue
		}

		// Check TPD limit
		if model.TpdLimit != nil && *model.TpdLimit > 0 {
			var dailyTokens int
			for _, data := range usage.Past24HoursTokenUsage {
				dailyTokens += data.CostToken
			}
			if dailyTokens >= *model.TpdLimit {
				usage.Exceeded = true
				continue // Skip this key
			}
		}

		if usage.Exceeded {
			continue
		}
		if usage.ProbablyExceeded {
			probablyAvailableKeys = append(probablyAvailableKeys, keyInfo)
			continue
		}
		availableKeys = append(availableKeys, keyInfo)
	}

	if len(availableKeys) == 0 {
		if len(probablyAvailableKeys) == 0 {
			return "", modelName, 0, fmt.Errorf("no available keys for model %s", modelName)
		}
		availableKeys = probablyAvailableKeys // Try probably exceeded keys
	}

	// Simple round-robin for now, can be improved
	keyToUse := availableKeys[0]
	usage := km.usage[modelName+"_"+keyToUse.Key]

	// Calculate delay based on TPM
	var past60sTokens int
	for _, data := range usage.Past60sTokenUsage {
		past60sTokens += data.CostToken
	}

	var delay time.Duration
	if past60sTokens > model.TpmLimit/2 { // Start delaying when half the limit is reached
		// A simple delay logic, can be more sophisticated
		excessTokens := past60sTokens - model.TpmLimit/2
		delay = time.Duration(float64(excessTokens)/float64(model.TpmLimit)*60) * time.Second
	}
	if past60sTokens > model.TpmLimit {
		delay = 60 * time.Second // Wait for a full minute
	}

	return keyToUse.Key, modelName, delay, nil
}

func (km *KeyManager) RecordUsage(modelName, key string, tokenCount int) {
	km.mutex.Lock()
	defer km.mutex.Unlock()

	usageKey := modelName + "_" + key
	usage, ok := km.usage[usageKey]
	if !ok {
		return
	}

	now := time.Now().Unix()
	newData := UsageData{
		Timestamp: int(now),
		CostToken: tokenCount,
	}

	usage.TotalTokenUse += tokenCount
	usage.TodayUsage += tokenCount
	usage.Past24HoursTokenUsage = append(usage.Past24HoursTokenUsage, newData)
	usage.JustHit429 = false // A successful request resets the flag
	UpdateLanguageModelUsage(usage, now)
}

func (km *KeyManager) HandleRateLimitError(modelName, key string) {
	km.mutex.Lock()
	defer km.mutex.Unlock()

	usageKey := modelName + "_" + key
	usage, ok := km.usage[usageKey]
	if !ok {
		return
	}

	UpdateLanguageModelUsage(usage, time.Now().Unix())

	// If daily usage is over 4.1M tokens, a 429 error means the quota is likely exhausted.
	if usage.TodayUsage >= 4100000 {
		usage.Exceeded = true
		log.Printf("Rate limit hit for model %s with key %s and daily usage is over 4.1M. Marked as 'exceeded'.", modelName, key[:4])
		return
	}

	// This is the core of the new logic.
	if usage.JustHit429 {
		// This is the second consecutive 429 error after a delay. The delay mechanism failed.
		// Disable the model for this key temporarily.
		usage.ProbablyExceeded = true
		usage.JustHit429 = false // Reset the flag
		log.Printf("Consecutive rate limit hit for model %s with key %s after delay. Marked as 'probably exceeded'.", modelName, key[:4])
	} else {
		// This is the first 429 error in a sequence. Set the flag.
		// The proxy handler will now call GetKey, which will enforce a delay.
		usage.JustHit429 = true
		log.Printf("Rate limit hit for model %s with key %s. Delay mechanism will be used. If the next attempt also fails, the model will be disabled.", modelName, key[:4])
	}
}

func (km *KeyManager) EnableModel(modelName, key string) {
	km.mutex.Lock()
	defer km.mutex.Unlock()

	usageKey := modelName + "_" + key
	usage, ok := km.usage[usageKey]
	if !ok {
		log.Printf("EnableModel: Usage key '%s' not found", usageKey)
		return
	}

	if usage.ProbablyExceeded {
		usage.ProbablyExceeded = false
		usage.JustHit429 = false // Also reset the flag
		log.Printf("Model %s for key %s has been re-enabled.", modelName, key[:4])
	}
}

func LoadConfig() (*KeyManagerConfig, error) {
	configPath := "config.json"
	if _, err := os.Stat(configPath); os.IsNotExist(err) {
		// Create default config
		defaultConfig := KeyManagerConfig{
			PriorityKeys:  []string{"PriorityKeysHere-Key1", "PriorityKeysHere-Key2"},
			SecondaryKeys: []string{"SecondaryKeysHere-Key1", "SecondaryKeysHere-Key2"},
			Models: map[string]LanguageModel{
				"gemini-1.5-pro-latest":   {TpmLimit: 250000, TpdLimit: func(i int) *int { return &i }(6000000)},
				"gemini-1.5-flash-latest": {TpmLimit: 250000, TpdLimit: nil},
			},
			ResetAfter:             "01:00",
			NextQuotaResetDatetime: time.Now().AddDate(0, 0, 1).Format("2006-01-02") + " 01:00",
			Timezone:               "UTC",
			DefaultModel:           "gemini-1.5-pro-latest",
		}
		configData, err := json.MarshalIndent(defaultConfig, "", "  ")
		if err != nil {
			return nil, fmt.Errorf("failed to marshal default config: %v", err)
		}
		if err := os.WriteFile(configPath, configData, 0644); err != nil {
			return nil, fmt.Errorf("failed to write default config: %v", err)
		}
	}

	configData, err := os.ReadFile(configPath)
	if err != nil {
		return nil, fmt.Errorf("failed to read config file: %v", err)
	}

	var config KeyManagerConfig
	if err := json.Unmarshal(configData, &config); err != nil {
		return nil, fmt.Errorf("failed to parse config file: %v", err)
	}

	for name, model := range config.Models {
		model.ModelName = name
		config.Models[name] = model
	}

	return &config, nil
}

func saveConfig(config *KeyManagerConfig) error {
	configData, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to marshal config for saving: %v", err)
	}
	if err := os.WriteFile("config.json", configData, 0644); err != nil {
		return fmt.Errorf("failed to write config to file: %v", err)
	}
	return nil
}

func LoadKeyUsage(config *KeyManagerConfig) (map[string]*LanguageModelUsage, error) {
	usagePath := "key_usage.json"

	// Create a new usage map based on the current config. This is the source of truth.
	newUsage := make(map[string]*LanguageModelUsage)
	allKeys := append(config.PriorityKeys, config.SecondaryKeys...)
	for modelName, model := range config.Models {
		for _, key := range allKeys {
			usageKey := modelName + "_" + key
			newUsage[usageKey] = &LanguageModelUsage{
				LanguageModel:         model,
				TotalTokenUse:         0,
				Past24HoursTokenUsage: []UsageData{}, // Initialize as empty slice
				ProbablyExceeded:      false,
				Exceeded:              false,
			}
		}
	}

	// Load existing usage data if it exists
	usageData, err := os.ReadFile(usagePath)
	if err != nil {
		if os.IsNotExist(err) {
			// File doesn't exist, so we'll just save the new one
			if err := saveUsageToFile(newUsage, usagePath); err != nil {
				return nil, err
			}
			return newUsage, nil
		}
		return nil, fmt.Errorf("failed to read usage file: %v", err)
	}

	if len(usageData) > 0 {
		var oldUsage map[string]*LanguageModelUsage
		if err := json.Unmarshal(usageData, &oldUsage); err == nil {
			// Copy old data into the new structure
			for usageKey, usage := range newUsage {
				if oldData, ok := oldUsage[usageKey]; ok {
					usage.TotalTokenUse = oldData.TotalTokenUse
					usage.TodayUsage = oldData.TodayUsage
					// Make sure Past24HoursTokenUsage is not nil
					if oldData.Past24HoursTokenUsage != nil {
						usage.Past24HoursTokenUsage = oldData.Past24HoursTokenUsage
					}
					usage.ProbablyExceeded = oldData.ProbablyExceeded
					usage.Exceeded = oldData.Exceeded
					// JustHit429 is a runtime-only field, so no need to load it.
				}
			}
		} else {
			log.Printf("Failed to parse usage file, reinitializing: %v", err)
		}
	}

	// Overwrite the old usage file with the cleaned, config-synced data
	if err := saveUsageToFile(newUsage, usagePath); err != nil {
		return nil, err
	}

	return newUsage, nil
}

func (km *KeyManager) SaveUsage() {
	km.mutex.Lock()
	defer km.mutex.Unlock()

	// Avoid saving too frequently
	if time.Since(km.lastSaved) < 10*time.Second {
		return
	}

	if err := saveUsageToFile(km.usage, "key_usage.json"); err != nil {
		log.Printf("Error saving usage data: %v", err)
	}
	km.lastSaved = time.Now()
	log.Println("Usage data saved.")
}

func saveUsageToFile(usage map[string]*LanguageModelUsage, path string) error {
	usageData, err := json.MarshalIndent(usage, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to marshal usage data: %v", err)
	}
	return os.WriteFile(path, usageData, 0644)
}

func UpdateLanguageModelUsage(usage *LanguageModelUsage, now int64) {
	// Filter out data older than 24 hours
	updated24HoursUsage := make([]UsageData, 0, len(usage.Past24HoursTokenUsage))
	for _, data := range usage.Past24HoursTokenUsage {
		if int64(data.Timestamp) >= now-86400 { // 24 hours in seconds
			updated24HoursUsage = append(updated24HoursUsage, data)
		}
	}
	usage.Past24HoursTokenUsage = updated24HoursUsage

	// Update past 60 seconds usage
	updated60sUsage := make([]UsageData, 0, len(usage.Past24HoursTokenUsage))
	for _, data := range usage.Past24HoursTokenUsage {
		if int64(data.Timestamp) >= now-60 { // 60 seconds
			updated60sUsage = append(updated60sUsage, data)
		}
	}
	usage.Past60sTokenUsage = updated60sUsage
}

func (km *KeyManager) GetStatus() *StatusData {
	km.mutex.Lock()
	defer km.mutex.Unlock()
	km.usageHistoryMutex.Lock()
	defer km.usageHistoryMutex.Unlock()

	now := time.Now().Unix()
	grandTotalTokens := 0
	grandTotalTodayUsage := 0
	keyUsageStatus := make(map[string]KeyStatus)
	rateLimitedKeys := make(map[string]bool)
	quotaExhaustedKeys := make(map[string]bool)
	unavailableKeys := make(map[string]bool)

	allKeys := append(km.config.PriorityKeys, km.config.SecondaryKeys...)
	modelOrder := make([]string, 0, len(km.config.Models))
	modelsConfig := make(map[string]ModelConfig)
	for name, model := range km.config.Models {
		modelOrder = append(modelOrder, name)
		modelsConfig[name] = ModelConfig{TpmLimit: model.TpmLimit}
	}
	sort.Strings(modelOrder) // Sort model names alphabetically

	for _, key := range allKeys {
		keyStatus := make(KeyStatus)
		for _, modelName := range modelOrder {
			usageKey := modelName + "_" + key
			usage, ok := km.usage[usageKey]
			if !ok {
				continue
			}

			UpdateLanguageModelUsage(usage, now)
			grandTotalTokens += usage.TotalTokenUse
			grandTotalTodayUsage += usage.TodayUsage

			var tokensLastMinute int
			for _, data := range usage.Past60sTokenUsage {
				tokensLastMinute += data.CostToken
			}

			keyStatus[modelName] = ModelUsageStatus{
				TokensLastMinute:      tokensLastMinute,
				TotalTokens:           usage.TotalTokenUse,
				TodayUsage:            usage.TodayUsage,
				IsTemporarilyDisabled: usage.ProbablyExceeded,
				DailyQuotaExceeded:    usage.Exceeded,
			}

			if usage.ProbablyExceeded {
				rateLimitedKeys[key] = true
			}
			if usage.Exceeded {
				quotaExhaustedKeys[key] = true
			}
		}
		keyUsageStatus[key] = keyStatus
	}

	// --- Chart Data Generation ---
	modelChartData := generateChartData(km.lastHourTokenUsage, now, modelOrder)
	keyChartData := generateChartData(km.lastHourKeyUsage, now, allKeys)

	// Active Key Model Chart Data
	currentMaskedKey := "None"
	currentRawKey := ""
	_, _, key, err := km.findBestKey(km.config.DefaultModel, now)
	if err == nil && key != "" {
		currentMaskedKey = key[:4] + "..." + key[len(key)-4:]
		currentRawKey = key
	}

	activeKeyModelUsage := make(map[string][]UsageData)
	if currentRawKey != "" {
		for _, modelName := range modelOrder {
			usageKey := modelName + "_" + currentRawKey
			if usage, ok := km.usage[usageKey]; ok {
				// This gives minute-by-minute data for the active key's models
				// We need to aggregate it per model for the chart
				// Let's build a temporary history for this
				modelHistory := make(map[int64]int)
				for _, dataPoint := range usage.Past24HoursTokenUsage {
					if int64(dataPoint.Timestamp) >= now-3600 {
						// Round timestamp to the nearest minute
						minuteTimestamp := (int64(dataPoint.Timestamp) / 60) * 60
						modelHistory[minuteTimestamp] += dataPoint.CostToken
					}
				}
				var historySlice []UsageData
				for ts, tokens := range modelHistory {
					historySlice = append(historySlice, UsageData{Timestamp: int(ts), CostToken: tokens})
				}
				// Sort by timestamp
				sort.Slice(historySlice, func(i, j int) bool {
					return historySlice[i].Timestamp < historySlice[j].Timestamp
				})
				activeKeyModelUsage[modelName] = historySlice
			}
		}
	}
	activeKeyModelChartData := generateChartData(activeKeyModelUsage, now, modelOrder)

	return &StatusData{
		GrandTotalTokens:        grandTotalTokens,
		GrandTotalTodayUsage:    grandTotalTodayUsage,
		CurrentMaskedKey:        currentMaskedKey,
		CurrentRawKey:           currentRawKey,
		KeyUsageStatus:          keyUsageStatus,
		PriorityKeys:            km.config.PriorityKeys,
		SecondaryKeys:           km.config.SecondaryKeys,
		RateLimitedKeys:         keysFromMap(rateLimitedKeys),
		QuotaExhaustedKeys:      keysFromMap(quotaExhaustedKeys),
		UnavailableKeys:         keysFromMap(unavailableKeys),
		ModelOrder:              modelOrder,
		ModelsConfig:            modelsConfig,
		ModelChartData:          modelChartData,
		KeyChartData:            keyChartData,
		ActiveKeyModelChartData: activeKeyModelChartData,
	}
}

func generateChartData(usageSource map[string][]UsageData, now int64, seriesOrder []string) ChartData {
	chartData := ChartData{
		Labels:   []string{},
		Datasets: []ChartDataset{},
	}

	// Generate all possible timestamps for the last hour (every minute)
	timestamps := make(map[int64]bool)
	allTimestampsSlice := make([]int64, 0, 60)
	for i := 59; i >= 0; i-- {
		ts := now - int64(i*60)
		minuteTimestamp := (ts / 60) * 60 // Round to the minute
		if !timestamps[minuteTimestamp] {
			timestamps[minuteTimestamp] = true
			allTimestampsSlice = append(allTimestampsSlice, minuteTimestamp)
		}
	}
	sort.Slice(allTimestampsSlice, func(i, j int) bool { return allTimestampsSlice[i] < allTimestampsSlice[j] })

	for _, ts := range allTimestampsSlice {
		chartData.Labels = append(chartData.Labels, time.Unix(ts, 0).Format("15:04"))
	}

	// Define a broader palette of colors
	modelColors := []string{
		"rgba(54, 162, 235, 1)", "rgba(255, 99, 132, 1)", "rgba(75, 192, 192, 1)",
		"rgba(255, 206, 86, 1)", "rgba(153, 102, 255, 1)", "rgba(255, 159, 64, 1)",
		"rgba(99, 255, 132, 1)", "rgba(235, 54, 162, 1)", "rgba(86, 255, 206, 1)",
		"rgba(102, 153, 255, 1)",
	}
	bgColors := []string{
		"rgba(54, 162, 235, 0.2)", "rgba(255, 99, 132, 0.2)", "rgba(75, 192, 192, 0.2)",
		"rgba(255, 206, 86, 0.2)", "rgba(153, 102, 255, 0.2)", "rgba(255, 159, 64, 0.2)",
		"rgba(99, 255, 132, 0.2)", "rgba(235, 54, 162, 0.2)", "rgba(86, 255, 206, 0.2)",
		"rgba(102, 153, 255, 0.2)",
	}

	colorIndex := 0
	for _, seriesName := range seriesOrder {
		history, ok := usageSource[seriesName]
		if !ok || len(history) == 0 {
			continue // Skip series with no data
		}

		// Check if there's any activity in the last hour
		hasRecentActivity := false
		for _, data := range history {
			if int64(data.Timestamp) >= now-3600 {
				hasRecentActivity = true
				break
			}
		}
		if !hasRecentActivity {
			continue
		}

		dataset := ChartDataset{
			Label:           seriesName,
			Data:            make([]int, len(allTimestampsSlice)),
			Fill:            true,
			BorderColor:     modelColors[colorIndex%len(modelColors)],
			BackgroundColor: bgColors[colorIndex%len(bgColors)],
			Tension:         0.4,
		}
		colorIndex++

		usageMap := make(map[int64]int)
		for _, data := range history {
			minuteTimestamp := (int64(data.Timestamp) / 60) * 60
			usageMap[minuteTimestamp] = data.CostToken
		}

		for j, ts := range allTimestampsSlice {
			if val, ok := usageMap[ts]; ok {
				dataset.Data[j] = val
			} else {
				dataset.Data[j] = 0
			}
		}
		chartData.Datasets = append(chartData.Datasets, dataset)
	}

	return chartData
}

func (km *KeyManager) findBestKey(modelName string, now int64) (string, time.Duration, string, error) {
	// This is a simplified, read-only version of GetKey logic for status reporting
	if _, ok := km.config.Models[modelName]; !ok {
		modelName = km.config.DefaultModel
	}
	model := km.config.Models[modelName]

	var availableKeys []KeyInfo
	var probablyAvailableKeys []KeyInfo

	for _, keyInfo := range km.keys {
		usageKey := modelName + "_" + keyInfo.Key
		usage, ok := km.usage[usageKey]
		if !ok {
			continue
		}

		// Create a temporary copy for checks to avoid locking
		tempUsage := *usage
		UpdateLanguageModelUsage(&tempUsage, now)

		if model.TpdLimit != nil && *model.TpdLimit > 0 {
			var dailyTokens int
			for _, data := range tempUsage.Past24HoursTokenUsage {
				dailyTokens += data.CostToken
			}
			if dailyTokens >= *model.TpdLimit {
				continue
			}
		}

		if tempUsage.Exceeded {
			continue
		}
		if tempUsage.ProbablyExceeded {
			probablyAvailableKeys = append(probablyAvailableKeys, keyInfo)
			continue
		}
		availableKeys = append(availableKeys, keyInfo)
	}

	if len(availableKeys) == 0 {
		if len(probablyAvailableKeys) == 0 {
			return "", 0, "", fmt.Errorf("no available keys for model %s", modelName)
		}
		availableKeys = probablyAvailableKeys
	}

	return availableKeys[0].Key, 0, availableKeys[0].Key, nil
}

func keysFromMap(m map[string]bool) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys) // Sort for consistent order
	return keys
}
