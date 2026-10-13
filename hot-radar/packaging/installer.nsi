; 热点雷达 Windows 安装程序（NSIS）。由 packaging/build.py 调用：
; makensis /INPUTCHARSET UTF8 /DVERSION=1.0.0 /DSRC=dist\HotRadar /DOUT=...exe /DICON=icon.ico installer.nsi
Unicode true
!include "MUI2.nsh"

!define APPNAME "热点雷达"
!define APPID "HotRadar"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}"

Name "${APPNAME} ${VERSION}"
OutFile "${OUT}"
InstallDir "$LOCALAPPDATA\Programs\${APPID}"
InstallDirRegKey HKCU "Software\${APPID}" "InstallDir"
RequestExecutionLevel user          ; 装在当前用户目录，不需要管理员权限
SetCompressor /SOLID lzma
BrandingText "${APPNAME} ${VERSION}（GPL-3.0）"
VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName" "${APPNAME}"
VIAddVersionKey /LANG=2052 "FileDescription" "${APPNAME} 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright" "GPL-3.0；基于开源项目 TrendRadar"

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APPID}.exe"
!define MUI_FINISHPAGE_RUN_TEXT "立即打开${APPNAME}"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${SRC}\_internal\project\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Section "Install"
  ; 覆盖安装前先结束正在运行的旧版本（主进程和它的工作进程都叫 HotRadar.exe）
  nsExec::Exec 'taskkill /F /IM ${APPID}.exe'
  SetOutPath "$INSTDIR"
  RMDir /r "$INSTDIR\_internal"
  File /r "${SRC}\*.*"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\${APPID}.exe"
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\${APPID}.exe"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\卸载${APPNAME}.lnk" "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "Software\${APPID}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "HotRadar（基于 TrendRadar）"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\${APPID}.exe"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  nsExec::Exec 'taskkill /F /IM ${APPID}.exe'
  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  ; 只删自己装的文件，防止用户把安装目录选在桌面等位置时误删其他文件
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\${APPID}.exe"
  Delete "$INSTDIR\安装说明.md"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTKEY}"
  DeleteRegKey HKCU "Software\${APPID}"
  ; 数据（%LOCALAPPDATA%\HotRadar：配置、关键词、密钥、历史数据和报告）保留，重新安装后都还在
SectionEnd
