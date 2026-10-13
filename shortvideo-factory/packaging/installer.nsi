; AI 短视频工厂 Windows 安装程序（NSIS）。由 packaging/build.py 调用：
; makensis /INPUTCHARSET UTF8 /DVERSION=1.0.0 /DSRC=dist\ShortVideoFactory /DOUT=...exe /DICON=icon.ico installer.nsi
; 装到 %LOCALAPPDATA%\Programs\ShortVideoFactory，不需要管理员权限；卸载保留用户数据（%LOCALAPPDATA%\ShortVideoFactory）。
Unicode true
!include "MUI2.nsh"

!define APPNAME "AI 短视频工厂"
!define APPID "ShortVideoFactory"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}"

Name "${APPNAME} ${VERSION}"
OutFile "${OUT}"
InstallDir "$LOCALAPPDATA\Programs\${APPID}"
InstallDirRegKey HKCU "Software\${APPID}" "InstallDir"
RequestExecutionLevel user          ; 装在当前用户目录，不需要管理员权限
SetCompressor /SOLID lzma
SetCompressorDictSize 64
BrandingText "${APPNAME} ${VERSION}"
VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName" "${APPNAME}"
VIAddVersionKey /LANG=2052 "FileDescription" "${APPNAME} 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright" "基于 MoneyPrinterTurbo（MIT）"

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APPID}.exe"
!define MUI_FINISHPAGE_RUN_TEXT "立即打开${APPNAME}"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

; 结束正在运行的本应用：启动器，以及安装目录里的随包 Python（生成界面后台）和 FFmpeg
!macro StopRunning
  nsExec::Exec 'taskkill /F /T /IM ${APPID}.exe'
  nsExec::Exec `powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process | Where-Object { $$_.Path -like '$INSTDIR\*' } | Stop-Process -Force -ErrorAction SilentlyContinue"`
  Sleep 800
!macroend

Section "Install"
  !insertmacro StopRunning
  SetOutPath "$INSTDIR"
  ; 覆盖安装：先删旧程序文件（不碰用户数据，用户数据不在安装目录）
  RMDir /r "$INSTDIR\_internal"
  RMDir /r "$INSTDIR\payload"
  File /r "${SRC}\*.*"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\${APPID}.exe"
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\${APPID}.exe"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\卸载${APPNAME}.lnk" "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "Software\${APPID}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "ShortVideoFactory"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\${APPID}.exe"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  !insertmacro StopRunning
  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  ; 只删自己装的文件，防止用户把安装目录选在桌面等位置时误删其他文件
  RMDir /r "$INSTDIR\_internal"
  RMDir /r "$INSTDIR\payload"
  Delete "$INSTDIR\${APPID}.exe"
  Delete "$INSTDIR\使用指南.md"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTKEY}"
  DeleteRegKey HKCU "Software\${APPID}"
  ; 用户数据（%LOCALAPPDATA%\ShortVideoFactory：设置、API Key、生成的视频）保留，重新安装后还在
SectionEnd
