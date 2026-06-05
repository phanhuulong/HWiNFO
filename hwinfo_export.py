"""
HWiNFO64 Automation - Export JSON + Upload
Flow:
  1. Launch HWiNFO64 → Start
  2. Close System Summary child window
  3. Click "Create a Report File" → JSON → Next → Finish
  4. Upload file lên server
     - Success → xóa file JSON
     - Fail    → move file vào thư mục failed/
"""

import os
import sys
import time
import subprocess
import ctypes
import logging
import shutil
from pathlib import Path

BASE_DIR   = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
HWINFO_EXE = BASE_DIR / "hwinfo_app" / "HWiNFO64.exe"
LOG_FILE   = BASE_DIR / "collector.log"

# =========================
# CONFIG
# =========================
UPLOAD_URL = "https://dkcme.pnt.edu.vn/api/upload/computer-settings"
PERSISTENT_FOLDER_NAME = "HWINFO_DATA"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)


def require_admin():
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return
    except Exception:
        pass
    log.info("Relaunching as admin...")
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, f'"{__file__}"', None, 1
    )
    sys.exit(0)


def kill_hwinfo():
    try:
        subprocess.run(["taskkill", "/f", "/im", "HWiNFO64.exe"], capture_output=True)
    except FileNotFoundError:
        # WinPE: taskkill may not be in PATH, use full path
        try:
            subprocess.run(
                [r"X:\Windows\System32\taskkill.exe", "/f", "/im", "HWiNFO64.exe"],
                capture_output=True,
            )
        except FileNotFoundError:
            log.warning("taskkill not found — cannot kill HWiNFO64")
    time.sleep(1)


def find_report(since: float) -> Path | None:
    for d in {BASE_DIR, HWINFO_EXE.parent}:
        for ext in ("*.json", "*.JSON"):
            for p in d.glob(ext):
                if p.stat().st_mtime > since and "collector" not in p.name.lower():
                    return p
    return None


def connect_app():
    from pywinauto import Application
    for i in range(20):
        time.sleep(1)
        try:
            app = Application(backend="uia").connect(path=str(HWINFO_EXE), timeout=2)
            log.info(f"Connected (attempt {i+1})")
            return app
        except Exception as e:
            log.debug(f"  connect {i+1}: {e}")
    raise RuntimeError("Cannot connect to HWiNFO64")


def get_main_window(app, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        wins = app.windows()
        for w in wins:
            if "hwinfo" in w.window_text().lower():
                return w
        time.sleep(1)
    raise RuntimeError("HWiNFO main window not found")


def close_system_summary(app, main):
    from pywinauto.keyboard import send_keys
    deadline = time.time() + 8  # Tăng thời gian chờ lên 8 giây (đề phòng WinPE load chậm)
    while time.time() < deadline:
        closed_something = False
        
        # 1. Tìm ở top-level
        for w in app.windows():
            t = w.window_text()
            if t and "summary" in t.lower():
                log.info(f"  Closing top-level: '{t}'")
                try:
                    w.set_focus()
                    send_keys("{ESC}")
                    time.sleep(0.5)
                    w.close()
                except Exception:
                    pass
                closed_something = True

        # 2. Tìm ở cửa sổ con (children)
        for child in main.descendants(control_type="Window"):
            t = child.window_text()
            if t and "summary" in t.lower():
                log.info(f"  Closing child: '{t}'")
                try:
                    child.set_focus()
                    send_keys("{ESC}")
                    time.sleep(0.5)
                    child.close()
                except Exception:
                    pass
                closed_something = True
                
        if closed_something:
            time.sleep(1)
            return  # Đã đóng thành công, thoát hàm
            
        time.sleep(1) # Chờ 1 giây rồi quét lại nếu chưa thấy System Summary hiện ra
        
    log.debug("  System Summary not open or already closed")


def find_child_dialog(app, main, keywords, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for w in app.windows():
            t = w.window_text()
            if t and any(k.lower() in t.lower() for k in keywords):
                log.info(f"  Dialog found (top-level): '{t}'")
                return w
        for ctrl in main.descendants(control_type="Window"):
            t = ctrl.window_text()
            if t and any(k.lower() in t.lower() for k in keywords):
                log.info(f"  Dialog found (child): '{t}'")
                return ctrl
        time.sleep(0.5)
    children = [c.window_text() for c in main.descendants(control_type="Window")]
    top_level = [w.window_text() for w in app.windows()]
    raise RuntimeError(f"Dialog {keywords} not found.\nTop-level: {top_level}\nChildren: {children}")


# =========================
# PERSISTENT STORAGE (USB Ventoy)
# =========================
def find_ventoy_drive() -> Path | None:
    """
    Quét các ổ đĩa C:-Z: (bỏ qua X: vì đó là RAM disk của WinPE)
    để tìm USB Ventoy. Nhận diện bằng:
      1. Có thư mục 'ventoy/' ở root
      2. Hoặc volume label chứa 'Ventoy'
    """
    for letter in "CDEFGHIJKLMNOPQRSTUVWYZ":  # skip X
        drive = Path(f"{letter}:\\")
        if not drive.exists():
            continue
        # Check 1: thư mục ventoy/ tồn tại
        if (drive / "ventoy").is_dir():
            log.info(f"  Found Ventoy USB at {letter}:\\ (ventoy/ folder detected)")
            return drive
        # Check 2: volume label (dùng vol command)
        try:
            result = subprocess.run(
                ["vol", f"{letter}:"],
                capture_output=True, encoding="utf-8", timeout=5,
                shell=True,
            )
            if "ventoy" in result.stdout.lower():
                log.info(f"  Found Ventoy USB at {letter}:\\ (volume label)")
                return drive
        except Exception:
            pass
    return None


def get_persistent_dir() -> Path | None:
    """
    Tìm hoặc tạo thư mục HWINFO_DATA trên USB Ventoy.
    Trả về Path nếu thành công, None nếu không tìm thấy USB.
    """
    drive = find_ventoy_drive()
    if not drive:
        log.warning("  No Ventoy USB found — cannot save persistent data")
        return None
    persistent = drive / PERSISTENT_FOLDER_NAME
    try:
        persistent.mkdir(exist_ok=True)
        log.info(f"  Persistent directory: {persistent}")
        return persistent
    except Exception as e:
        log.error(f"  Cannot create {persistent}: {e}")
        return None


def save_log_to_usb():
    """
    Copy collector.log ra USB Ventoy để debug sau khi reboot.
    """
    pdir = get_persistent_dir()
    if pdir and LOG_FILE.exists():
        try:
            dest = pdir / LOG_FILE.name
            shutil.copy2(str(LOG_FILE), str(dest))
            log.info(f"  Log saved to USB: {dest}")
        except Exception as e:
            log.warning(f"  Could not save log to USB: {e}")


# =========================
# UPLOAD
# =========================
def upload(report: Path) -> bool:
    """
    Upload file JSON lên server dùng curl (có sẵn trên Windows 10+).
    Không cần cài thêm requests hay lib nào.
    Trả về True nếu server trả "success".
    """
    log.info(f"Uploading: {report.name} → {UPLOAD_URL}")
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-X", "POST", UPLOAD_URL,
                "-H", "accept: application/json",
                "-H", "Content-Type: multipart/form-data",
                "-F", f"file=@{report}",
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        log.info(f"  curl stdout: {stdout}")
        if stderr:
            log.debug(f"  curl stderr: {stderr}")

        if '"success"' in stdout.lower() or '"status":"success"' in stdout.lower():
            log.info("  Upload SUCCESS")
            return True
        else:
            log.warning(f"  Upload FAILED — unexpected response: {stdout}")
            return False

    except subprocess.TimeoutExpired:
        log.error("  Upload FAILED — timeout")
        return False
    except Exception as e:
        log.error(f"  Upload FAILED — {e}")
        return False


def handle_report(report: Path):
    """Upload → xóa nếu thành công, save vào USB Ventoy nếu thất bại."""
    # Server check endswith('.json') — HWiNFO xuất .JSON (uppercase) → rename
    if report.suffix == ".JSON":
        renamed = report.with_suffix(".json")
        try:
            report.rename(renamed)
            report = renamed
            log.info(f"  Renamed to: {report.name}")
        except Exception as e:
            log.warning(f"  Rename failed: {e}")

    success = upload(report)
    if success:
        try:
            report.unlink()
            log.info(f"  Deleted: {report.name}")
        except Exception as e:
            log.warning(f"  Could not delete {report.name}: {e}")
    else:
        # Upload thất bại → lưu vào USB Ventoy để không mất dữ liệu
        persistent = get_persistent_dir()
        if persistent:
            dest = persistent / report.name
            if dest.exists():
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                dest = persistent / f"{report.stem}_{timestamp}{report.suffix}"
                counter = 1
                while dest.exists():
                    dest = persistent / f"{report.stem}_{timestamp}_{counter}{report.suffix}"
                    counter += 1
            try:
                shutil.move(str(report), str(dest))
                log.warning(f"  Saved to USB: {dest}")
            except Exception as e:
                log.error(f"  Could not save to USB: {e}")
        else:
            log.error(f"  LOST: {report.name} — no USB found, file will be lost on reboot!")
    return success


# ========================================================
# TERMINAL UI NOTIFICATION WITH ANSI COLORS & BLOCK ART
# ========================================================
def show_result(success: bool, message: str):
    # Kích hoạt chế độ Virtual Terminal xử lý mã màu ANSI trên Windows CMD/Terminal
    os.system('') 
    
    GREEN = "\033[92m"
    RED = "\033[91m"
    RESET = "\033[0m"
    BOLD = "\033[1m"
    
    print("\n" + "=" * 60)
    if success:
        print(f"{GREEN}{BOLD}")
        print("           ██╗")
        print("          ██╔╝")
        print("    ██╗  ██╔╝     UPLOAD THÀNH CÔNG!")
        print("    ╚██╗██╔╝")
        print("     ╚███╔╝       Dữ liệu máy tính đã gửi lên hệ thống.")
        print("      ╚══╝")
        print(f"{RESET}{GREEN}")
        print(f"[+] Chi tiết: {message.replace('\n', ' ')}")
    else:
        print(f"{RED}{BOLD}")
        print("    ██╗  ██╗")
        print("    ╚██╗██╔╝")
        print("     ╚███╔╝       UPLOAD THẤT BẠI!")
        print("     ██╔██╗")
        print("    ██╔╝ ██╗      Quá trình cấu hình gặp sự cố.")
        print("    ╚═╝  ╚═╝")
        print(f"{RESET}{RED}")
        print(f"[!] Lý do: {message}")
    print(f"{RESET}" + "=" * 60 + "\n")
    
    # Giữ Terminal mở chặn không cho tắt ngay lập tức
    input("Nhấn phím [ENTER] để kết thúc và đóng cửa sổ này...")


# =========================
# MAIN AUTOMATION
# =========================
def run():
    from pywinauto.keyboard import send_keys

    if not HWINFO_EXE.exists():
        log.error(f"Not found: {HWINFO_EXE}")
        sys.exit(1)

    kill_hwinfo()
    t_start = time.time()

    log.info(f"Launching: {HWINFO_EXE}")
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "open", str(HWINFO_EXE), None, str(HWINFO_EXE.parent), 1
    )
    if ret <= 32:
        raise RuntimeError(f"ShellExecuteW failed: {ret}")

    app = connect_app()

    # --- BƯỚC 1: Startup dialog → Start ---
    log.info("Step 1: Startup dialog...")
    startup = get_main_window(app, timeout=15)
    for btn in startup.descendants(control_type="Button"):
        if btn.window_text().strip().lower() == "start":
            btn.click_input()
            log.info("  Clicked: Start")
            break
    else:
        startup.set_focus()
        send_keys("{ENTER}")

    # --- BƯỚC 2: Đợi toolbar load ---
    log.info("Step 2: Waiting for main window to load...")
    main = None
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(1)
        try:
            w = get_main_window(app, timeout=2)
            btns = [b.window_text() for b in w.descendants(control_type="Button")]
            if any("report" in b.lower() or "create" in b.lower() for b in btns):
                log.info(f"  Ready: '{w.window_text()}'")
                main = w
                break
        except Exception:
            pass
    if not main:
        raise RuntimeError("Main window did not load within 30s")

    # --- BƯỚC 3: Đóng System Summary ---
    log.info("Step 3: Closing System Summary...")
    close_system_summary(app, main)

    # --- BƯỚC 4: Click "Create a Report File" ---
    log.info("Step 4: Create a Report File...")
    main.set_focus()
    time.sleep(0.3)

    clicked = False
    for btn in main.descendants(control_type="Button"):
        label = btn.window_text().strip()
        if "create" in label.lower() and "report" in label.lower():
            btn.click_input()
            log.info(f"  Clicked: '{label}'")
            clicked = True
            break
    if not clicked:
        toolbar = main.child_window(control_type="ToolBar")
        btns = toolbar.descendants(control_type="Button")
        btns[1].click_input()
        log.info(f"  Toolbar[1]: '{btns[1].window_text()}'")

    # --- BƯỚC 5: Create Logfile dialog → JSON → Next → Finish ---
    log.info("Step 5: Create Logfile dialog...")
    dlg = find_child_dialog(app, main, ["logfile", "create logfile", "report type"], timeout=15)

    # HWiNFO radio buttons có thể là custom control, không phải RadioButton chuẩn
    # Dump tất cả controls để tìm đúng type
    log.debug("  All dialog controls:")
    for ctrl in dlg.descendants():
        t  = ctrl.window_text().strip()
        ct = ctrl.element_info.control_type
        if t:
            log.debug(f"    [{ct}] '{t}'")

    json_selected = False

    # Thử 1: RadioButton chuẩn
    for ctrl in dlg.descendants(control_type="RadioButton"):
        if ctrl.window_text().strip().lower() == "json":
            ctrl.click_input()
            log.info("  Selected (RadioButton): JSON")
            json_selected = True
            break

    # Thử 2: CheckBox
    if not json_selected:
        for ctrl in dlg.descendants(control_type="CheckBox"):
            if ctrl.window_text().strip().lower() == "json":
                ctrl.click_input()
                log.info("  Selected (CheckBox): JSON")
                json_selected = True
                break

    # Thử 3: bất kỳ control nào text == "JSON"
    if not json_selected:
        for ctrl in dlg.descendants():
            if ctrl.window_text().strip().lower() == "json":
                try:
                    ctrl.click_input()
                    log.info(f"  Selected ({ctrl.element_info.control_type}): JSON")
                    json_selected = True
                    break
                except Exception as e:
                    log.debug(f"  click failed: {e}")

    if not json_selected:
        log.warning("  JSON control not found — HWiNFO will use last saved format")

    time.sleep(0.3)

    for btn in dlg.descendants(control_type="Button"):
        if "next" in btn.window_text().lower():
            btn.click_input()
            log.info(f"  Clicked: '{btn.window_text()}'")
            break

    time.sleep(0.5)

    for _ in range(8):
        for btn in dlg.descendants(control_type="Button"):
            if "finish" in btn.window_text().lower():
                btn.click_input()
                log.info(f"  Clicked: '{btn.window_text()}'")
                break
        else:
            time.sleep(0.5)
            continue
        break

    # --- BƯỚC 6: Đợi file JSON ---
    log.info("Step 6: Waiting for JSON file...")
    report = None
    for i in range(30):
        time.sleep(1)
        report = find_report(t_start)
        if report:
            log.info(f"  Found: {report.name} ({report.stat().st_size:,} bytes)")
            break
        log.debug(f"  waiting... {i+1}s")

    # Đóng HWiNFO
    log.info("Closing HWiNFO...")
    try:
        main.close()
        time.sleep(2)
    except Exception:
        pass
    kill_hwinfo()

    return report


if __name__ == "__main__":
    require_admin()
    success = False
    message = ""
    try:
        report = run()
        if not report:
            log.error("FAILED: no JSON file generated")
            message = "Không tạo được file báo cáo.\nKiểm tra collector.log để biết thêm chi tiết."
        else:
            ok = handle_report(report)
            if ok:
                success = True
                message = "Dữ liệu cấu hình máy đã được\nghi nhận thành công lên hệ thống."
            else:
                message = "Không thể upload lên server.\nFile báo cáo đã được lưu vào USB."

    except Exception as e:
        log.error(f"Fatal: {e}", exc_info=True)
        message = f"Lỗi không xác định:\n{e}"

    # Luôn copy log ra USB để debug sau reboot
    save_log_to_usb()

    show_result(success, message)
