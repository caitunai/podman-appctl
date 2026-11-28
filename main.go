package main

import (
	"os"

	"github.com/caitunai/appnet/cmd"
)

func main() {
	if err := cmd.Execute(); err != nil {
		os.Exit(1)
	}
}
