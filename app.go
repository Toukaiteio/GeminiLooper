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

func main() {
	keyManager, err := NewKeyManager()
	if err != nil {
		log.Fatalf("Failed to create key manager: %v", err)
	}

	r := gin.Default()
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

	r.GET("/status", func(c *gin.Context) {
		c.HTML(http.StatusOK, "status.html", nil)
	})

	r.GET("/api/status_data", func(c *gin.Context) {
		statusData := keyManager.GetStatus()
		c.JSON(http.StatusOK, statusData)
	})

	r.POST("/api/test_key", testKeyHandler())

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

		for i := 0; i < 5; i++ { // Retry loop
			apiKey, modelName, delay, err = km.GetKey(modelName)
			if err != nil {
				c.JSON(http.StatusTooManyRequests, gin.H{"error": fmt.Sprintf("Failed to get API key: %v", err)})
				return
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
				continue // Retry with a new key
			}

			// Other errors
			respBody, _ := io.ReadAll(resp.Body)
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

func testKeyHandler() gin.HandlerFunc {
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
