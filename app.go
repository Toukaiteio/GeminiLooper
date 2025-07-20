package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
)

type GeminiResponse struct {
	Candidates []struct {
		// ... other fields
	} `json:"candidates"`
	UsageMetadata struct {
		PromptTokenCount     int `json:"promptTokenCount"`
		CandidatesTokenCount int `json:"candidatesTokenCount"`
		TotalTokenCount      int `json:"totalTokenCount"`
	} `json:"usageMetadata"`
}

type OpenAIUsage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	TotalTokens      int `json:"total_tokens"`
}

type OpenAIResponse struct {
	Usage OpenAIUsage `json:"usage"`
}

type OllamaRequest struct {
	Model    string `json:"model"`
	Messages []struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	} `json:"messages"`
	Stream *bool `json:"stream,omitempty"`
}

type GeminiRequest struct {
	Contents []struct {
		Role  string `json:"role"`
		Parts []struct {
			Text string `json:"text"`
		} `json:"parts"`
	} `json:"contents"`
}

type OllamaStreamResponse struct {
	Model     string    `json:"model"`
	CreatedAt time.Time `json:"created_at"`
	Response  string    `json:"response"`
	Done      bool      `json:"done"`
}

func main() {
	keyManager, err := NewKeyManager()
	if err != nil {
		log.Fatalf("Failed to create key manager: %v", err)
	}

	gin.SetMode(gin.ReleaseMode)
	gin.DefaultWriter = io.Discard
	r := gin.New()
	r.Use(gin.Recovery())
	r.LoadHTMLFiles("templates/status.html")

	target, err := url.Parse("https://generativelanguage.googleapis.com")
	if err != nil {
		log.Fatal(err)
	}

	proxy := httputil.NewSingleHostReverseProxy(target)

	proxy.ModifyResponse = func(resp *http.Response) error {
		// We will handle the response in the handler
		return nil
	}

	r.POST("/v1beta/models/:model_name", proxyHandler(keyManager, target))
	r.POST("/v1/*path", openAIProxyHandler(keyManager, target))
	r.POST("/api/chat", ollamaProxyHandler(keyManager, target))

	r.GET("/status", func(c *gin.Context) {
		c.HTML(http.StatusOK, "status.html", nil)
	})

	r.GET("/api/status_data", func(c *gin.Context) {
		statusData := keyManager.GetStatus()
		c.JSON(http.StatusOK, statusData)
	})

	r.POST("/api/test_key", testKeyHandler(keyManager))
	r.POST("/api/enable_model", enableModelHandler(keyManager))

	srv := &http.Server{
		Addr:    ":48888",
		Handler: r,
	}

	go func() {
		// service connections
		log.Println("Starting server on :48888")
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen: %s\n", err)
		}
	}()

	// Wait for interrupt signal to gracefully shutdown the server with
	// a timeout of 5 seconds.
	quit := make(chan os.Signal, 1)
	// kill (no param) default send syscall.SIGTERM
	// kill -2 is syscall.SIGINT
	// kill -9 is syscall.SIGKILL but can't be caught, so don't need to add it
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("Shutting down server...")

	// The context is used to inform the server it has 5 seconds to finish
	// the requests it is currently handling
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Fatal("Server forced to shutdown:", err)
	}

	log.Println("Calling KeyManager Stop function...")
	keyManager.Stop()
	log.Println("Server exiting")
}

func proxyHandler(km *KeyManager, target *url.URL) gin.HandlerFunc {
	return func(c *gin.Context) {
		fullModelName := c.Param("model_name")
		if fullModelName == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "Model not specified"})
			return
		}
		// Split model name from action, e.g., "gemini-1.5-pro-latest:streamGenerateContent"
		parts := strings.Split(fullModelName, ":")
		modelName := parts[0]
		action := ""
		if len(parts) > 1 {
			action = parts[1]
		}

		var apiKey string
		var delay time.Duration
		var err error
		var initialModelName = modelName

		// Get the initial key
		apiKey, modelName, delay, err = km.GetKey(initialModelName)
		if err != nil {
			c.JSON(http.StatusTooManyRequests, gin.H{"error": fmt.Sprintf("Failed to get initial API key: %v", err)})
			return
		}

		for i := 0; i < 5; i++ { // Retry loop
			// On subsequent retries, we might need a new key if the current one was disabled.
			if i > 0 {
				apiKey, modelName, delay, err = km.GetKey(initialModelName)
				if err != nil {
					c.JSON(http.StatusTooManyRequests, gin.H{"error": fmt.Sprintf("Failed to get API key for retry: %v", err)})
					return
				}
			}

			if delay > 0 {
				time.Sleep(delay)
			}

			// Read body
			body, err := io.ReadAll(c.Request.Body)
			if err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to read request body"})
				return
			}
			c.Request.Body = io.NopCloser(bytes.NewBuffer(body)) // Restore body

			// Construct the correct path including the action
			path := fmt.Sprintf("/v1beta/models/%s:%s", modelName, action)
			if action == "" {
				path = fmt.Sprintf("/v1beta/models/%s", modelName)
			}

			// Create new request
			proxyReq, err := http.NewRequest(c.Request.Method, c.Request.URL.String(), bytes.NewBuffer(body))
			if err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to create proxy request"})
				return
			}

			proxyReq.Header = c.Request.Header
			proxyReq.URL.Scheme = target.Scheme
			proxyReq.URL.Host = target.Host
			proxyReq.URL.Path = path

			// Set the content length to the size of the new body
			proxyReq.ContentLength = int64(len(body))

			// Add API key
			q := proxyReq.URL.Query()
			q.Set("key", apiKey)
			proxyReq.URL.RawQuery = q.Encode()

			// Send request
			client := &http.Client{}
			resp, err := client.Do(proxyReq)
			if err != nil {
				c.JSON(http.StatusBadGateway, gin.H{"error": "Failed to send request to upstream server"})
				return
			}
			defer resp.Body.Close()

			// Handle response
			if resp.StatusCode == http.StatusOK {
				// Copy headers
				for k, v := range resp.Header {
					c.Writer.Header()[k] = v
				}
				c.Writer.WriteHeader(resp.StatusCode)

				// For streaming, we need to read and write simultaneously
				// We also need to capture the response for token counting
				var respBodyBuffer bytes.Buffer
				teeReader := io.TeeReader(resp.Body, &respBodyBuffer)

				// Stream the response to the client
				_, err := io.Copy(c.Writer, teeReader)
				if err != nil {
					log.Printf("Error streaming response to client: %v", err)
					// Don't return here, still try to record usage
				}

				// Now, process the captured response
				// Note: For streaming responses, the full JSON might be a series of JSON objects.
				// This simple Unmarshal will only get the last one if it's a stream of concatenated JSONs.
				// A more robust solution would be to parse the stream properly.
				// However, for Gemini, the usage data is usually at the end.
				var geminiResp GeminiResponse
				if err := json.Unmarshal(respBodyBuffer.Bytes(), &geminiResp); err == nil {
					km.RecordUsage(modelName, apiKey, geminiResp.UsageMetadata.TotalTokenCount)
				} else {
					// It might be a streaming response with multiple JSON objects
					// Try to find the usage data in the raw string
					// This is a fallback and might not be perfect
					content := respBodyBuffer.String()
					if strings.Contains(content, "usageMetadata") {
						// A simplified parser to extract totalTokenCount
						// This is not robust, but a decent fallback.
						// A proper implementation should handle JSON stream parsing.
						// Example stream part: ... "usageMetadata": { "promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 3 } }
						re := regexp.MustCompile(`"totalTokenCount":\s*(\d+)`)
						matches := re.FindStringSubmatch(content)
						if len(matches) > 1 {
							if tokenCount, err := strconv.Atoi(matches[1]); err == nil {
								km.RecordUsage(modelName, apiKey, tokenCount)
							}
						}
					}
				}

				return
			}

			if resp.StatusCode == http.StatusTooManyRequests {
				km.HandleRateLimitError(modelName, apiKey)
				log.Printf("Rate limit hit for model %s with key %s. Retrying...", modelName, apiKey[:4])
				// The key is now flagged. The next call to GetKey will either return the same key with a delay,
				// or a new key if the current one was disabled after repeated failures.
				continue
			}

			if resp.StatusCode == http.StatusServiceUnavailable {
				log.Printf("Service unavailable (503) for model %s with key %s. Retrying in 5 seconds...", modelName, apiKey[:4])
				time.Sleep(5 * time.Second)
				continue // Retry with the same key
			}

			// Other errors
			respBody, _ := io.ReadAll(resp.Body)
			log.Printf("Gemini native proxy: upstream server returned error: %d %s", resp.StatusCode, string(respBody))
			c.Data(resp.StatusCode, resp.Header.Get("Content-Type"), respBody)
			return
		}

		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "Service unavailable after multiple retries"})
	}
}

type TestRequest struct {
	APIKey    string `json:"api_key"`
	ModelName string `json:"model_name"`
}

func testKeyHandler(km *KeyManager) gin.HandlerFunc {
	return func(c *gin.Context) {
		var req TestRequest
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request body"})
			return
		}

		// Construct a minimal request to the Gemini API
		requestBody := `{
			"contents": [{"parts":[{"text": "test"}]}]
		}`

		url := fmt.Sprintf("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s", req.ModelName, req.APIKey)

		httpReq, err := http.NewRequest("POST", url, strings.NewReader(requestBody))
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to create request"})
			return
		}
		httpReq.Header.Set("Content-Type", "application/json")

		client := &http.Client{Timeout: 20 * time.Second}
		resp, err := client.Do(httpReq)
		if err != nil {
			c.JSON(http.StatusBadGateway, gin.H{"error": fmt.Sprintf("Failed to send request to upstream server: %v", err)})
			return
		}
		defer resp.Body.Close()

		// We only care about the status code
		c.JSON(http.StatusOK, gin.H{"status_code": resp.StatusCode})
	}
}

func enableModelHandler(km *KeyManager) gin.HandlerFunc {
	return func(c *gin.Context) {
		var req TestRequest
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request body"})
			return
		}

		km.EnableModel(req.ModelName, req.APIKey)
		c.JSON(http.StatusOK, gin.H{"status": "ok"})
	}
}

func openAIProxyHandler(km *KeyManager, target *url.URL) gin.HandlerFunc {
	return func(c *gin.Context) {
		body, err := io.ReadAll(c.Request.Body)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to read request body"})
			return
		}
		c.Request.Body = io.NopCloser(bytes.NewBuffer(body)) // Restore for safety/consistency

		var bodyJSON struct {
			Model string `json:"model"`
		}
		if err := json.Unmarshal(body, &bodyJSON); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request body, cannot parse model name"})
			return
		}
		clientModelName := bodyJSON.Model
		if clientModelName == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "Model not specified in request body"})
			return
		}

		var apiKey string
		var returnedModelName string
		var delay time.Duration
		var initialModelName = clientModelName

		// Get the initial key
		apiKey, returnedModelName, delay, err = km.GetKey(initialModelName)
		if err != nil {
			c.JSON(http.StatusTooManyRequests, gin.H{"error": fmt.Sprintf("Failed to get initial API key: %v", err)})
			return
		}

		for i := 0; i < 5; i++ { // Retry loop
			// On subsequent retries, we might need a new key if the current one was disabled.
			if i > 0 {
				apiKey, returnedModelName, delay, err = km.GetKey(initialModelName)
				if err != nil {
					c.JSON(http.StatusTooManyRequests, gin.H{"error": fmt.Sprintf("Failed to get API key for retry: %v", err)})
					return
				}
			}

			if delay > 0 {
				time.Sleep(delay)
			}

			// Construct the correct path
			originalPath := c.Param("path")
			path := "/v1beta/openai" + originalPath

			// Create new request
			proxyReq, err := http.NewRequest(c.Request.Method, c.Request.URL.String(), bytes.NewBuffer(body))
			if err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to create proxy request"})
				return
			}

			proxyReq.Header = c.Request.Header
			proxyReq.URL.Scheme = target.Scheme
			proxyReq.URL.Host = target.Host
			proxyReq.URL.Path = path
			proxyReq.ContentLength = int64(len(body))

			// Add API key
			q := proxyReq.URL.Query()
			q.Set("key", apiKey)
			proxyReq.URL.RawQuery = q.Encode()

			// Send request
			client := &http.Client{}
			resp, err := client.Do(proxyReq)
			if err != nil {
				c.JSON(http.StatusBadGateway, gin.H{"error": "Failed to send request to upstream server"})
				return
			}
			defer resp.Body.Close()

			// Handle response
			if resp.StatusCode == http.StatusOK {
				for k, v := range resp.Header {
					c.Writer.Header()[k] = v
				}
				c.Writer.WriteHeader(resp.StatusCode)

				var respBodyBuffer bytes.Buffer
				teeReader := io.TeeReader(resp.Body, &respBodyBuffer)

				_, err := io.Copy(c.Writer, teeReader)
				if err != nil {
					log.Printf("Error streaming response to client: %v", err)
				}

				var openAIResp OpenAIResponse
				if err := json.Unmarshal(respBodyBuffer.Bytes(), &openAIResp); err == nil {
					if openAIResp.Usage.TotalTokens > 0 {
						km.RecordUsage(returnedModelName, apiKey, openAIResp.Usage.TotalTokens)
					}
				} else {
					content := respBodyBuffer.String()
					if strings.Contains(content, `"usage"`) {
						re := regexp.MustCompile(`"total_tokens":\s*(\d+)`)
						matches := re.FindStringSubmatch(content)
						if len(matches) > 1 {
							if tokenCount, err := strconv.Atoi(matches[1]); err == nil {
								km.RecordUsage(returnedModelName, apiKey, tokenCount)
							}
						}
					}
				}
				return
			}

			if resp.StatusCode == http.StatusTooManyRequests {
				km.HandleRateLimitError(returnedModelName, apiKey)
				log.Printf("Rate limit hit for model %s with key %s. Retrying...", returnedModelName, apiKey[:4])
				// The key is now flagged. The next call to GetKey will either return the same key with a delay,
				// or a new key if the current one was disabled after repeated failures.
				continue
			}

			if resp.StatusCode == http.StatusServiceUnavailable {
				log.Printf("Service unavailable (503) for model %s with key %s. Retrying in 5 seconds...", returnedModelName, apiKey[:4])
				time.Sleep(5 * time.Second)
				continue // Retry with the same key
			}

			// Other errors
			respBody, _ := io.ReadAll(resp.Body)
			log.Printf("OpenAI proxy: upstream server returned error: %d %s", resp.StatusCode, string(respBody))
			c.Data(resp.StatusCode, resp.Header.Get("Content-Type"), respBody)
			return
		}

		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "Service unavailable after multiple retries"})
	}
}

func ollamaProxyHandler(km *KeyManager, target *url.URL) gin.HandlerFunc {
	return func(c *gin.Context) {
		bodyBytes, err := io.ReadAll(c.Request.Body)
		if err != nil {
			log.Printf("Ollama proxy: failed to read request body: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "failed to read request body"})
			return
		}
		c.Request.Body = io.NopCloser(bytes.NewBuffer(bodyBytes)) // Restore body

		var ollamaReq OllamaRequest
		if err := c.ShouldBindJSON(&ollamaReq); err != nil {
			log.Printf("Ollama proxy: failed to bind JSON: %v. Body: %s", err, string(bodyBytes))
			c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request body"})
			return
		}

		if ollamaReq.Model == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "Model not specified in request body"})
			return
		}

		// Translate Ollama request to Gemini request
		geminiReq := GeminiRequest{
			Contents: []struct {
				Role  string `json:"role"`
				Parts []struct {
					Text string `json:"text"`
				} `json:"parts"`
			}{},
		}

		// Translate and merge messages
		for _, msg := range ollamaReq.Messages {
			role := msg.Role
			if role == "assistant" {
				role = "model"
			} else if role == "system" {
				// Gemini API expects alternating user/model roles, so we'll treat the system role as a user role.
				role = "user"
			}
			// Gemini API requires alternating roles (user, model, user, model...)
			// We merge consecutive messages from the same role.
			if len(geminiReq.Contents) > 0 && geminiReq.Contents[len(geminiReq.Contents)-1].Role == role {
				// Merge with the previous message
				lastContent := &geminiReq.Contents[len(geminiReq.Contents)-1]
				lastContent.Parts[0].Text += "\n" + msg.Content
			} else {
				// Add a new message
				newContent := struct {
					Role  string `json:"role"`
					Parts []struct {
						Text string `json:"text"`
					} `json:"parts"`
				}{
					Role: role,
					Parts: []struct {
						Text string `json:"text"`
					}{{Text: msg.Content}},
				}
				geminiReq.Contents = append(geminiReq.Contents, newContent)
			}
		}

		// Gemini API requires the conversation to start with a "user" role.
		// We'll remove any leading "model" messages.
		if len(geminiReq.Contents) > 0 && geminiReq.Contents[0].Role == "model" {
			geminiReq.Contents = geminiReq.Contents[1:]
		}

		if len(geminiReq.Contents) == 0 {
			c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request: No user messages found after processing."})
			return
		}

		var apiKey, modelName string
		var delay time.Duration

		for i := 0; i < 5; i++ { // Retry loop
			// Get API key
			apiKey, modelName, delay, err = km.GetKey(ollamaReq.Model)
			if err != nil {
				c.JSON(http.StatusTooManyRequests, gin.H{"error": fmt.Sprintf("Failed to get API key: %v", err)})
				return
			}

			if delay > 0 {
				log.Printf("Ollama proxy: Delaying request for %v due to TPM limit", delay)
				time.Sleep(delay)
			}

			// Marshal the new Gemini request body
			geminiBody, err := json.Marshal(geminiReq)
			if err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to marshal Gemini request body"})
				return
			}

			// Determine if streaming is requested
			isStreaming := ollamaReq.Stream != nil && *ollamaReq.Stream

			action := "generateContent"
			if isStreaming {
				action = "streamGenerateContent"
			}

			// Construct the upstream URL
			path := fmt.Sprintf("/v1beta/models/%s:%s", modelName, action)
			upstreamURL := *target
			upstreamURL.Path = path
			q := upstreamURL.Query()
			q.Set("key", apiKey)
			upstreamURL.RawQuery = q.Encode()

			// Create the request to the upstream server
			proxyReq, err := http.NewRequest(c.Request.Method, upstreamURL.String(), bytes.NewBuffer(geminiBody))
			if err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to create proxy request"})
				return
			}

			proxyReq.Header.Set("Content-Type", "application/json")
			proxyReq.Header.Set("Accept", "application/json")

			// Send the request
			client := &http.Client{}
			resp, err := client.Do(proxyReq)
			if err != nil {
				c.JSON(http.StatusBadGateway, gin.H{"error": "Failed to send request to upstream server"})
				return
			}
			defer resp.Body.Close()

			if resp.StatusCode == http.StatusOK {
				// Set headers for streaming
				c.Writer.Header().Set("Content-Type", "application/x-ndjson")
				c.Writer.Header().Set("Cache-Control", "no-cache")
				c.Writer.Header().Set("Connection", "keep-alive")
				c.Writer.WriteHeader(resp.StatusCode)

				if isStreaming {
					// Handle streaming response by reading all at once, then processing.
					body, err := io.ReadAll(resp.Body)
					if err != nil {
						log.Printf("Ollama proxy: failed to read streaming response body: %v", err)
						// We can't send a JSON error because headers are already written.
						return
					}

					lines := strings.Split(string(body), "\n")
					for _, line := range lines {
						if strings.HasPrefix(line, "data: ") {
							jsonData := strings.TrimPrefix(line, "data: ")
							if len(strings.TrimSpace(jsonData)) == 0 {
								continue
							}
							var geminiChunk struct {
								Candidates []struct {
									Content struct {
										Parts []struct {
											Text string `json:"text"`
										} `json:"parts"`
									} `json:"content"`
								} `json:"candidates"`
							}
							if err := json.Unmarshal([]byte(jsonData), &geminiChunk); err == nil {
								if len(geminiChunk.Candidates) > 0 && len(geminiChunk.Candidates[0].Content.Parts) > 0 {
									responseText := geminiChunk.Candidates[0].Content.Parts[0].Text
									ollamaResp := OllamaStreamResponse{
										Model:     ollamaReq.Model,
										CreatedAt: time.Now(),
										Response:  responseText,
										Done:      false,
									}
									jsonResp, _ := json.Marshal(ollamaResp)
									fmt.Fprintln(c.Writer, string(jsonResp))
									c.Writer.Flush()
								}
							}
						}
					}
					// Send final done message
					ollamaResp := OllamaStreamResponse{
						Model:     ollamaReq.Model,
						CreatedAt: time.Now(),
						Response:  "",
						Done:      true,
					}
					jsonResp, _ := json.Marshal(ollamaResp)
					fmt.Fprintln(c.Writer, string(jsonResp))
					c.Writer.Flush()
				} else {
					// Handle non-streaming response
					body, _ := io.ReadAll(resp.Body)
					var geminiResp GeminiResponse
					if err := json.Unmarshal(body, &geminiResp); err == nil {
						km.RecordUsage(modelName, apiKey, geminiResp.UsageMetadata.TotalTokenCount)
						// Translate to Ollama format
						var fullText strings.Builder
						// for _, cand := range geminiResp.Candidates {
						// 	// For simplicity, we'll just concatenate the text from all parts and candidates.
						// 	// A more sophisticated approach might handle different candidate choices.
						// 	// fullText.WriteString(cand.Content.Parts[0].Text)
						// }
						// Create a single response object that mimics Ollama's non-streaming response.
						// This part is complex and depends on the exact format expected by the client.
						// We'll send a simplified response for now.
						c.JSON(http.StatusOK, gin.H{"model": ollamaReq.Model, "response": fullText.String(), "done": true})
					} else {
						c.Data(resp.StatusCode, resp.Header.Get("Content-Type"), body)
					}
				}
				return // Success, exit loop
			}

			if resp.StatusCode == http.StatusTooManyRequests {
				km.HandleRateLimitError(modelName, apiKey)
				log.Printf("Ollama proxy: Rate limit hit for model %s with key %s. Retrying...", modelName, apiKey[:4])
				continue // Retry with a new key
			}

			if resp.StatusCode == http.StatusServiceUnavailable {
				log.Printf("Ollama proxy: Service unavailable (503) for model %s with key %s. Retrying in 5 seconds...", modelName, apiKey[:4])
				time.Sleep(5 * time.Second)
				continue // Retry with the same key
			}

			// Other errors
			respBodyBytes, _ := io.ReadAll(resp.Body)
			log.Printf("Ollama proxy: upstream server returned error: %d %s", resp.StatusCode, string(respBodyBytes))
			c.Data(resp.StatusCode, resp.Header.Get("Content-Type"), respBodyBytes)
			return // Exit on other errors
		}

		// If loop finishes, all retries failed
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "Service unavailable after multiple retries"})
	}
}
