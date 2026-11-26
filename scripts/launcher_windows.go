// Windows launcher: no Go runtime installation is required by users.
// Rebuild using scripts/build_launcher.py; keep stdout reserved for MCP.
package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"
)

func main() {
	program, err := os.Executable()
	if err != nil {
		os.Exit(1)
	}
	for _, candidate := range [][]string{{"py", "-3"}, {"python"}, {"python3"}} {
		path, err := exec.LookPath(candidate[0])
		if err != nil {
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		probeArgs := append(append([]string{}, candidate[1:]...), "-c", "import sys; sys.exit(sys.version_info < (3, 10))")
		probe := exec.CommandContext(ctx, path, probeArgs...)
		probe.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
		err = probe.Run()
		cancel()
		if err != nil {
			continue
		}
		args := append(append([]string{}, candidate[1:]...), "-X", "utf8", filepath.Join(filepath.Dir(program), "run.py"))
		args = append(args, os.Args[1:]...)
		child := exec.Command(path, args...)
		child.Stdin, child.Stdout, child.Stderr = os.Stdin, os.Stdout, os.Stderr
		child.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
		if err := child.Run(); err != nil {
			if code, ok := err.(*exec.ExitError); ok {
				os.Exit(code.ExitCode())
			}
			fmt.Fprintln(os.Stderr, "Mogoo Codex Next could not start Python.")
			os.Exit(1)
		}
		return
	}
	fmt.Fprintln(os.Stderr, "Mogoo Codex Next requires Python 3.10+ on PATH (py -3, python or python3).")
	os.Exit(1)
}
