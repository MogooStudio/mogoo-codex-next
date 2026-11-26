"""Rebuild the dependency-free Windows launcher from its checked-in Go source."""
import os
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parent
env = dict(os.environ, GOOS="windows", GOARCH="amd64", CGO_ENABLED="0", GOTOOLCHAIN="go1.26.6", GOPROXY="off")
subprocess.run(["go", "build", "-trimpath", "-buildvcs=false", "-ldflags=-s -w -H windowsgui -buildid=",
                "-o", str(root / "launch.exe"), str(root / "launcher_windows.go")], env=env, check=True)
