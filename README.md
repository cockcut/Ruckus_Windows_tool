# HSITX Ruckus Technical Tool (Windows Native)

PHP / Linux / Expect 없이 **Windows에서 프로그램처럼** 사용하는 Ruckus AP 관리 도구입니다.

원래 `Ruckus_script_for_Private_SERVER` 의 웹 포털 메뉴를 콘솔 메뉴로 옮기고,  
Expect 스크립트 핵심 기능(AP SSH 접속 → 프롬프트 대기 → 명령 전송)을 **Python + Paramiko**로 재구현했습니다.

---

## 주요 특징

- **PHP 불필요** – 순수 Python 콘솔 프로그램
- **Expect 대체** – `modules/ssh_helper.py`에서 프롬프트 단위로 명령을 보내고 응답을 기다림
- 원래 메뉴 구조 유지 (1~11번)
- CSV로 다수 AP 일괄 처리
- 결과 CSV 자동 저장 (`results/` 폴더)
- 나중에 **PyInstaller**로 `.exe` 하나로 묶을 수 있음

---

## 설치 방법 (Windows)

### 1. Python 설치
- https://www.python.org/downloads/ 에서 **Python 3.10 이상** 설치
- 설치 시 **"Add Python to PATH"** 체크 필수

### 2. 의존성 설치
```powershell
cd Ruckus_Windows_Tool
python -m pip install -r requirements.txt
```

### 3. 실행
```powershell
python main.py
```

---

## 메뉴 설명

| 번호 | 기능 | 상태 |
|------|------|------|
| 1~5  | SmartZone / Unleashed / DPSK / PSK API | 뼈대만 (추후 확장) |
| **6** | **AP IP 변경 / SZ 연동 / 공장초기화 등** | **동작 (SSH 자동화)** |
| 7~9  | 펌웨어 업그레이드 관련 | 뼈대만 |
| 10   | ICX SNMP ARP | 뼈대만 |
| **11** | **OUI 조회** | **동작** |
| **12** | **단일 AP SSH 테스트** | **동작 (디버그용)** |

---

## 메뉴 6 사용법 (핵심)

1. `samples/ap_sample.csv` 를 복사해서 실제 AP 정보로 수정
2. 메뉴에서 **6** 선택
3. 원하는 작업 선택
   - `connect_sz` : SZ(SCG) IP 설정
   - `changeip` : AP IP 변경
   - `devicename` : 호스트명 변경
   - `sz_devicename_changeip` : 위 3가지 동시
   - `reboot` / `factory_reset`
4. CSV 경로 입력 후 실행

### CSV 형식 (8컬럼)

```csv
current_ip,username,password,new_ip,subnet,gateway,sz_ip,hostname
192.168.1.10,admin,password123,192.168.1.20,255.255.255.0,192.168.1.1,10.0.0.5,AP-Lobby
```

- 작업에 필요 없는 컬럼은 비워도 됩니다.
- `#` 으로 시작하는 줄은 주석으로 무시됩니다.

---

## Expect → Paramiko 대응 방식

원래 Expect 코드:

```tcl
send "set scg ip 10.0.0.5\r"
expect {
    -re "OK\r\n.*rkscli" { 성공 }
    timeout { 실패 }
}
```

Python (`ssh_helper.py`)에서는:

```python
ok, output = ssh.run("set scg ip 10.0.0.5")
# 내부적으로 정규식 "OK[\s\S]*?rkscli" 가 나타날 때까지 대기
```

로그인 시 비밀번호 실패 → `sp-admin` 재시도, Unleashed 감지 후 스킵 등  
원래 스크립트의 분기 로직도 최대한 재현했습니다.

---

## 단일 AP 테스트 (메뉴 12)

실제 장비에 바로 붙여서 명령을 시험해볼 수 있습니다.

```
선택 > 12
AP IP > 192.168.1.10
...
rkscli> get boarddata
rkscli> get scg
rkscli>          ← 빈 줄 입력 시 종료
```

---

## .exe 로 만들기 (선택)

```powershell
pip install pyinstaller
pyinstaller --onefile --console --name RuckusTool main.py
```

생성된 `dist/RuckusTool.exe` 를 다른 PC에 복사해서 실행할 수 있습니다.  
(`samples/` 폴더와 함께 두는 것을 권장)

---

## 다음 확장 가능한 부분

- 메뉴 7, 9 : 펌웨어 업그레이드 Expect 스크립트 → Paramiko로 이식
- 메뉴 1~5 : 원래 PHP의 API 호출을 `requests`로 재구현
- 메뉴 8 : fw.sh 의 HTML 생성 로직을 Python으로 이식
- GUI 버전 (tkinter / customtkinter)

필요한 기능을 말씀해 주시면 이어서 채워 드리겠습니다.
