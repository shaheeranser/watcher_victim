package main

import (
	"fmt"
	"net/http"
	"os"
	"time"
)

func crash() {
	var inventory map[string]int
	inventory["widget"] = 1
}

func port() string {
	if value := os.Getenv("VICTIM_GO_PORT"); value != "" {
		return value
	}
	return "8081"
}

func logInfo(msg string) {
	fmt.Printf("%s INFO victim-go %s\n", time.Now().UTC().Format(time.RFC3339), msg)
}

func main() {
	mux := http.NewServeMux()

	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("content-type", "application/json")
		fmt.Fprintln(w, `{"status":"ok"}`)
	})

	mux.HandleFunc("/panic", func(w http.ResponseWriter, r *http.Request) {
		go crash()
		time.Sleep(100 * time.Millisecond)
		w.WriteHeader(http.StatusAccepted)
	})

	addr := ":" + port()
	logInfo("listening on " + addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		logInfo("listen failed: " + err.Error())
		os.Exit(1)
	}
}
