#define MyAppName      "大气激光雷达散射特性仿真"
#define MyAppVersion   "1.1"
#define MyAppExeName   "LidarSim.exe"

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}

; ── Portable / zero-footprint installation ─────────────────────────────────
; No registry keys, no Start-Menu group, no uninstaller entry.
; User's machine state is 100 % untouched outside the chosen directory.
; To uninstall: simply delete the installation folder.
PrivilegesRequired=lowest
CreateUninstallRegKey=no
UpdateUninstallLogAppName=no
; Let the user choose any directory they like (no forced AppData path).
; 注意：安装路径不能包含中文或其他非英文字符（如 C:\软件\LidarSim 会导致
; conda-unpack 和 tar 解压失败）。请使用纯英文路径，例如 C:\LidarSim。
DefaultDirName={autopf}\LidarSim
DisableProgramGroupPage=yes
CreateAppDir=yes

OutputDir=dist
OutputBaseFilename=LidarSimSetup

; Use uncompressed source tarballs and let Inno Setup LZMA compress everything
; in one pass for a better overall ratio.
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern

[Files]
; ── Launcher ───────────────────────────────────────────────────────────────
Source: "dist\LidarSim.exe"; DestDir: "{app}"; Flags: ignoreversion

; ── Project source ─────────────────────────────────────────────────────────
Source: "..\..\app\*";        DestDir: "{app}\app";  Flags: recursesubdirs ignoreversion; Excludes: "__pycache__\*;*.pyc"
Source: "..\..\src\*";        DestDir: "{app}\src";  Flags: recursesubdirs ignoreversion; Excludes: "__pycache__\*;*.pyc"
; Only computation scripts are shipped — no documentation files.
Source: "..\..\temp\lidar_1d\lidar_1d_simulation.py"; DestDir: "{app}\temp\lidar_1d"; Flags: ignoreversion
Source: "..\..\temp\lidar_1d\make_final_figures.py";   DestDir: "{app}\temp\lidar_1d"; Flags: ignoreversion
Source: "..\..\temp\lidar_1d\high_altitude_aerosol.py"; DestDir: "{app}\temp\lidar_1d"; Flags: ignoreversion
Source: "..\..\temp\lidar_1d\julia\*"; DestDir: "{app}\temp\lidar_1d\julia"; Flags: recursesubdirs ignoreversion
Source: "..\..\pixi.toml";    DestDir: "{app}";      Flags: ignoreversion
Source: "..\..\pixi.lock";    DestDir: "{app}";      Flags: ignoreversion skipifsourcedoesntexist
Source: "..\..\README.md";    DestDir: "{app}";      Flags: ignoreversion skipifsourcedoesntexist

; ── Pre-built conda environments (uncompressed tarballs — LZMA'd by Inno) ──
Source: "dist\gui_packed.tar"; DestDir: "{app}\_packs"; Flags: ignoreversion
Source: "dist\mie_packed.tar"; DestDir: "{app}\_packs"; Flags: ignoreversion

; ── Portable Julia runtime ──────────────────────────────────────────────────
Source: "dist\julia\*"; DestDir: "{app}\julia"; Flags: recursesubdirs ignoreversion; Excludes: "share\julia\test\*;share\julia\base\test\*"
; julia_depot: 包含运行时 native 库（FLINT/OpenBLAS32/Wigxjpf/OpenSpecFun）。
; compiled/ 由用户机 postinstall.ps1 现场生成，确保 .ji 缓存路径正确。
Source: "dist\julia_depot\*"; DestDir: "{app}\julia_depot"; Flags: recursesubdirs ignoreversion; \
  Excludes: "packages\*\test\*;packages\*\.github\*;compiled\*"

; ── Hidden cache store seeds (not user-visible) ─────────────────────────────
Source: "dist\cache_store\*"; DestDir: "{app}\temp\lidar_1d\cache_store"; Flags: recursesubdirs ignoreversion skipifsourcedoesntexist

; ── Default result set (shipped snapshot for first display + cache seed) ────
Source: "dist\default_result\*"; DestDir: "{app}\temp\lidar_1d\default_result"; Flags: recursesubdirs ignoreversion skipifsourcedoesntexist
Source: "dist\outputs_high_altitude\*"; DestDir: "{app}\temp\lidar_1d\outputs_high_altitude"; Flags: recursesubdirs ignoreversion skipifsourcedoesntexist

; ── Local history snapshot (for reproducible demo/test states) ──────────────
Source: "dist\run_history\*"; DestDir: "{app}\temp\lidar_1d\run_history"; Flags: recursesubdirs ignoreversion skipifsourcedoesntexist
Source: "dist\runtime_state\*"; DestDir: "{app}\temp\lidar_1d\runtime_state"; Flags: recursesubdirs ignoreversion skipifsourcedoesntexist

; ── Post-install helper (deleted automatically after it runs) ───────────────
Source: "postinstall.ps1"; DestDir: "{app}"; Flags: ignoreversion deleteafterinstall

[Icons]
; Desktop shortcut — optional, created only when user ticks the box.
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "在桌面创建快捷方式（可选，不写入注册表）"; GroupDescription: "附加图标:"; Flags: unchecked

[Run]
; Unpack conda environments and fix relocatable paths — this is the slow step.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -File ""{app}\postinstall.ps1"" ""{app}"""; \
  StatusMsg: "Setting up Python environments (3-5 min)..."; \
  Flags: waituntilterminated
