; Сценарий Inno Setup. Только латиница в тексте файла.
#define AppName "Product Scraper"
#define AppVersion "1.0.0"
#define AppExe "ProductScraper.exe"

[Setup]
; AppId — уникальный номер программы, оставь как есть
AppId={{6F1B3A52-8C4D-4E27-9A0B-3D5C7E91F2A4}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\ProductScraper
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=ProductScraper-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExe}

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\ProductScraper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent