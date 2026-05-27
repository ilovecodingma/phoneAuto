# Phone Controller

ADB 기반 Android 폰 원격 제어/모니터링 GUI (Tkinter).

## Features

- 실시간 미러링 (H.264 / PNG 모드)
- 시스템 모니터 (CPU/MEM/온도/배터리/CPU 코어별 freq, 프로세스 목록)
- 매크로 레코더 (TAP/SWIPE/KEY/TEXT 녹화 → detached `.sh` 실행, 케이블 분리 후에도 동작)
- PC 키보드 → 폰 입력 패스스루 (한글은 ADBKeyboard IME 경유)
- logcat/dumpsys 뷰어 (프로세스 우클릭)

## 요구사항

- Windows 10/11 x64
- Android Platform Tools (`%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe`)
- USB 디버깅 활성화된 Android 기기

## 설치

### MSI 인스톨러 (권장)

`dist/PhoneController-1.0.0-win64.msi` 더블클릭 → 설치 → 시작 메뉴 "Phone Controller" 실행.

### 소스에서 실행

```bash
pip install pillow av
python gui.py
```

## 빌드 (MSI 재생성)

```bash
pip install cx_Freeze
python setup_msi.py bdist_msi
# → dist/PhoneController-<version>-win64.msi
```

## 파일

- `gui.py` — 메인 GUI
- `daemon.sh`, `handler.sh` — 폰에 푸시되는 TCP 데몬 (포트 8889)
- `ADBKeyboard.apk` — 한글 입력용 IME
- `setup_msi.py` — cx_Freeze MSI 빌드 스크립트
- `test_*.py` — 개발용 테스트 스크립트
