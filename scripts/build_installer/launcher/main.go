package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

var (
	modKernel32               = syscall.NewLazyDLL("kernel32.dll")
	procSetThreadExecState    = modKernel32.NewProc("SetThreadExecutionState")
)

// esFlags: prevent display sleep and system sleep while the app is running.
const (
	esContinuous      = 0x80000000
	esSystemRequired  = 0x00000001
	esDisplayRequired = 0x00000002
)

// keepAwake calls SetThreadExecutionState so the OS will not sleep or
// dim the display while this process is running. The effect is automatically
// reverted when the process exits.
func keepAwake() {
	procSetThreadExecState.Call(
		uintptr(esContinuous | esSystemRequired | esDisplayRequired),
	)
}

// setenv replaces an existing key in env slice or appends a new one.
func setenv(env []string, key, val string) []string {
	prefix := strings.ToUpper(key) + "="
	for i, e := range env {
		if strings.ToUpper(e[:min(len(e), len(prefix))]) == prefix {
			env[i] = key + "=" + val
			return env
		}
	}
	return append(env, key+"="+val)
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func diagnosticsEnabled() bool {
	return strings.TrimSpace(os.Getenv("LIDAR_DIAGNOSTICS")) == "1"
}

func ensureDir(path string) {
	_ = os.MkdirAll(path, 0755)
}

func appendJSONL(path string, payload map[string]any) {
	ensureDir(filepath.Dir(path))
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()
	buf, err := json.Marshal(payload)
	if err != nil {
		return
	}
	_, _ = f.Write(append(buf, '\n'))
}

func diagEvent(runDir string, stage string, status string, elapsedMs float64, payload map[string]any) {
	if runDir == "" {
		return
	}
	record := map[string]any{
		"run_id":     filepath.Base(runDir),
		"ts":         time.Now().UTC().Format(time.RFC3339Nano),
		"component":  "launcher",
		"stage":      stage,
		"status":     status,
		"elapsed_ms": elapsedMs,
		"payload":    payload,
	}
	appendJSONL(filepath.Join(runDir, "timeline.jsonl"), record)
	appendJSONL(filepath.Join(runDir, "launcher", "launcher_timeline.jsonl"), record)
}

func defaultRunID() string {
	return fmt.Sprintf("launcher_%s", time.Now().Format("20060102_150405"))
}

func diagnosticsRunDir(installDir string) (string, string) {
	if !diagnosticsEnabled() {
		return "", ""
	}
	runID := strings.TrimSpace(os.Getenv("LIDAR_DIAGNOSTICS_RUN_ID"))
	if runID == "" {
		runID = defaultRunID()
	}
	root := strings.TrimSpace(os.Getenv("LIDAR_DIAGNOSTICS_ROOT"))
	if root == "" {
		root = filepath.Join(installDir, "diagnostics")
	}
	runDir := filepath.Join(root, "runs", runID)
	ensureDir(filepath.Join(runDir, "launcher"))
	return runID, runDir
}

func writeSessionFiles(runDir string) {
	if runDir == "" {
		return
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	session := map[string]any{
		"run_id":     filepath.Base(runDir),
		"mode":       os.Getenv("LIDAR_DIAGNOSTICS_MODE"),
		"component":  "launcher",
		"root_dir":   filepath.Dir(filepath.Dir(runDir)),
		"run_dir":    runDir,
		"created_at": now,
	}
	writeJSON(filepath.Join(runDir, "session.json"), session)
	if _, err := os.Stat(filepath.Join(runDir, "summary.json")); err != nil {
		writeJSON(filepath.Join(runDir, "summary.json"), map[string]any{
			"run_id":     filepath.Base(runDir),
			"mode":       os.Getenv("LIDAR_DIAGNOSTICS_MODE"),
			"status":     "running",
			"components": map[string]any{},
			"created_at": now,
			"updated_at": now,
		})
	}
}

func writeJSON(path string, payload map[string]any) {
	ensureDir(filepath.Dir(path))
	buf, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return
	}
	_ = os.WriteFile(path, buf, 0644)
}

func main() {
	// Prevent the OS from sleeping or dimming display during long computations.
	keepAwake()
	startupT0 := time.Now()

	exe, err := os.Executable()
	if err != nil {
		writeLog("cannot determine executable path: " + err.Error())
		return
	}
	installDir := filepath.Dir(exe)
	_, diagRunDir := diagnosticsRunDir(installDir)
	writeSessionFiles(diagRunDir)
	if diagRunDir != "" {
		diagEvent(diagRunDir, "launcher_started", "begin", 0, map[string]any{"install_dir": installDir})
	}

	guiPython := filepath.Join(installDir, ".pixi", "envs", "gui", "python.exe")
	miePython := filepath.Join(installDir, ".pixi", "envs", "mie", "python.exe")
	script := filepath.Join(installDir, "app", "demo_ui.py")

	env := os.Environ()
	// Redirect all per-tool home directories into the install directory so
	// nothing is ever written outside it at runtime.
	env = setenv(env, "PIXI_HOME", filepath.Join(installDir, ".pixi_home"))
	env = setenv(env, "JULIA_DEPOT_PATH", filepath.Join(installDir, "julia_depot"))
	env = setenv(env, "JULIA_BINDIR", filepath.Join(installDir, "julia", "bin"))
	env = setenv(env, "JULIA_EXE", filepath.Join(installDir, "julia", "bin", "julia.exe"))
	env = setenv(env, "JULIA_PKG_PRECOMPILE_AUTO", "0")
	env = setenv(env, "LIDAR_MIE_PYTHON", miePython)
	env = setenv(env, "LIDAR_INSTALL_DIR", installDir)
	if diagRunDir != "" {
		env = setenv(env, "LIDAR_DIAGNOSTICS", "1")
		env = setenv(env, "LIDAR_DIAGNOSTICS_RUN_ID", filepath.Base(diagRunDir))
		env = setenv(env, "LIDAR_DIAGNOSTICS_ROOT", filepath.Dir(filepath.Dir(diagRunDir)))
		if strings.TrimSpace(os.Getenv("LIDAR_DIAGNOSTICS_MODE")) != "" {
			env = setenv(env, "LIDAR_DIAGNOSTICS_MODE", os.Getenv("LIDAR_DIAGNOSTICS_MODE"))
		}
		if strings.TrimSpace(os.Getenv("LIDAR_DIAGNOSTICS_PHASE")) != "" {
			env = setenv(env, "LIDAR_DIAGNOSTICS_PHASE", os.Getenv("LIDAR_DIAGNOSTICS_PHASE"))
		}
		diagEvent(diagRunDir, "runtime_env_prepared", "ok", 0, map[string]any{
			"gui_python":        guiPython,
			"mie_python":        miePython,
			"script":            script,
			"diagnostics_phase": strings.TrimSpace(os.Getenv("LIDAR_DIAGNOSTICS_PHASE")),
		})
	}

	// Prepend julia/bin so lidar_1d_simulation.py can call julia directly.
	juliaBin := filepath.Join(installDir, "julia", "bin")
	if old := os.Getenv("PATH"); old != "" {
		env = setenv(env, "PATH", juliaBin+string(os.PathListSeparator)+old)
	} else {
		env = setenv(env, "PATH", juliaBin)
	}

	cmd := exec.Command(guiPython, script)
	cmd.Dir = installDir
	cmd.Env = env
	if diagRunDir != "" {
		diagEvent(diagRunDir, "gui_launch_prepare", "begin", 0, map[string]any{"cwd": installDir})
	}

	// Hide the console window of the child process (CREATE_NO_WINDOW).
	// The launcher itself is already built with -H windowsgui so it has no
	// console window; this flag ensures child processes inherit the same behaviour.
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: 0x08000000, // CREATE_NO_WINDOW
		HideWindow:    true,
	}

	// Log stdout+stderr to a file beside the launcher for troubleshooting.
	// Append mode preserves logs across restarts; a timestamp header separates sessions.
	logPath := filepath.Join(installDir, "LidarSim.log")
	if lf, err := os.OpenFile(logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644); err == nil {
		lf.WriteString("\n=== session started ===\n")
		cmd.Stdout = lf
		cmd.Stderr = lf
		defer lf.Close()
	}

	if diagRunDir != "" {
		diagEvent(diagRunDir, "gui_process_started", "begin", 0, map[string]any{"log_path": logPath})
	}
	if err := cmd.Run(); err != nil {
		if diagRunDir != "" {
			diagEvent(diagRunDir, "gui_process_exit", "error", float64(time.Since(startupT0).Milliseconds()), map[string]any{"error": err.Error()})
		}
		writeLog("startup error: " + err.Error())
		return
	}
	if diagRunDir != "" {
		diagEvent(diagRunDir, "gui_process_exit", "ok", float64(time.Since(startupT0).Milliseconds()), map[string]any{"status": "success"})
	}
}

func writeLog(msg string) {
	exe, _ := os.Executable()
	logPath := filepath.Join(filepath.Dir(exe), "LidarSim.log")
	f, err := os.OpenFile(logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()
	f.WriteString(msg + "\n")
}

// Ensure unsafe is used (needed for syscall.NewLazyDLL on some Go versions).
var _ = unsafe.Sizeof(0)
