[Setup]
AppName=Retail Capture
AppVersion=1.0
DefaultDirName={autopf}\RetailCapture
DefaultGroupName=Retail Capture
OutputDir=installer
OutputBaseFilename=RetailCapture-Setup
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest

[Files]
Source: "dist\RetailCapture\*"; DestDir: "{app}"; Flags: recursesubdirs

[Icons]
Name: "{group}\Retail Capture"; Filename: "{app}\RetailCapture.exe"
Name: "{autodesktop}\Retail Capture"; Filename: "{app}\RetailCapture.exe"

[Run]
Filename: "{app}\RetailCapture.exe"; Description: "Launch Retail Capture"; Flags: nowait postinstall skipifsilent
