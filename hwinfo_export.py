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
    try:
        import uiautomation as auto
    except ImportError:
        log.error("uiautomation not installed. Please run: pip install uiautomation")
        sys.exit(1)
    
    # Giảm thời gian chờ mặc định của uiautomation để tránh treo script
    auto.SetGlobalSearchTimeout(2.0)
    log.info("Connected to uiautomation backend")
    return auto


def get_main_window(timeout=30):
    import uiautomation as auto
    deadline = time.time() + timeout
    while time.time() < deadline:
        # Quét các cửa sổ top-level chứa chữ hwinfo
        for w in auto.GetRootControl().GetChildren():
            if w.ControlType == auto.ControlType.WindowControl and w.Name and "hwinfo" in w.Name.lower():
                return w
        time.sleep(1)
    raise RuntimeError("HWiNFO main window not found")


def close_system_summary():
    import uiautomation as auto
    deadline = time.time() + 8  # Tăng thời gian chờ lên 8 giây (đề phòng WinPE load chậm)
    while time.time() < deadline:
        closed_something = False
        
        # Quét các cửa sổ đang mở để tìm System Summary
        for w in auto.GetRootControl().GetChildren():
            if w.ControlType == auto.ControlType.WindowControl and w.Name and "summary" in w.Name.lower():
                log.info(f"  Closing System Summary: '{w.Name}'")
                try:
                    w.SetFocus()
                    auto.SendKeys('{ESC}')
                    time.sleep(0.5)
                except Exception:
                    pass
                closed_something = True

        if closed_something:
            time.sleep(1)
            return  # Đã đóng thành công
            
        time.sleep(1)
        
    log.debug("  System Summary not open or already closed")


def find_child_dialog(main, keywords, timeout=15):
    import uiautomation as auto
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 1. Tìm ở top-level
        for w in auto.GetRootControl().GetChildren():
            if w.ControlType == auto.ControlType.WindowControl and w.Name and any(k.lower() in w.Name.lower() for k in keywords):
                log.info(f"  Dialog found (top-level): '{w.Name}'")
                return w
                
        # 2. Tìm bên trong main window
        if main and main.Exists(0, 0):
            for child in main.GetChildren():
                if child.ControlType == auto.ControlType.WindowControl and child.Name and any(k.lower() in child.Name.lower() for k in keywords):
                    log.info(f"  Dialog found (child): '{child.Name}'")
                    return child
                    
        time.sleep(0.5)
    raise RuntimeError(f"Dialog {keywords} not found.")


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
    import uiautomation as auto

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

    connect_app()

    # --- BƯỚC 1: Startup dialog → Start ---
    log.info("Step 1: Startup dialog...")
    startup = get_main_window(timeout=15)
    
    # Click start button
    start_btn = startup.ButtonControl(searchDepth=3, RegexName="(?i)start|run")
    if start_btn.Exists(1, 1):
        start_btn.Click()
        log.info(f"  Clicked: '{start_btn.Name}'")
    else:
        startup.SetFocus()
        auto.SendKeys("{ENTER}")

    # --- BƯỚC 2: Đợi toolbar load ---
    log.info("Step 2: Waiting for main window to load...")
    main = None
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(1)
        try:
            w = get_main_window(timeout=2)
            # Check if report button exists
            if w.ButtonControl(searchDepth=4, RegexName="(?i).*report.*|.*create.*").Exists(1, 1):
                log.info(f"  Ready: '{w.Name}'")
                main = w
                break
        except Exception:
            pass
    if not main:
        raise RuntimeError("Main window did not load within 30s")

    # --- BƯỚC 3: Đóng System Summary ---
    log.info("Step 3: Closing System Summary...")
    close_system_summary()

    # --- BƯỚC 4: Click "Create a Report File" ---
    log.info("Step 4: Create a Report File...")
    main.SetFocus()
    time.sleep(0.3)

    report_btn = main.ButtonControl(searchDepth=4, RegexName="(?i).*create.*report.*")
    if report_btn.Exists(1, 1):
        report_btn.Click()
        log.info(f"  Clicked: '{report_btn.Name}'")
    else:
        # Fallback to toolbar
        toolbar = main.ToolBarControl()
        if toolbar.Exists(1, 1):
            btns = toolbar.GetChildren()
            if len(btns) > 1:
                btns[1].Click()
                log.info(f"  Toolbar[1]: '{btns[1].Name}'")

    # --- BƯỚC 5: Create Logfile dialog → JSON → Next → Finish ---
    log.info("Step 5: Create Logfile dialog...")
    dlg = find_child_dialog(main, ["logfile", "create logfile", "report type"], timeout=15)

    json_selected = False
    
    json_ctrl = dlg.Control(searchDepth=3, Name="JSON")
    if json_ctrl.Exists(1, 1):
        json_ctrl.Click()
        log.info(f"  Selected ({json_ctrl.ControlType}): JSON")
        json_selected = True

    if not json_selected:
        log.warning("  JSON control not found — HWiNFO will use last saved format")

    time.sleep(0.3)

    next_btn = dlg.ButtonControl(searchDepth=3, RegexName="(?i).*next.*")
    if next_btn.Exists(1, 1):
        next_btn.Click()
        log.info(f"  Clicked: '{next_btn.Name}'")

    time.sleep(0.5)

    for _ in range(8):
        finish_btn = dlg.ButtonControl(searchDepth=3, RegexName="(?i).*finish.*")
        if finish_btn.Exists(1, 1):
            finish_btn.Click()
            log.info(f"  Clicked: '{finish_btn.Name}'")
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
        main.SetFocus()
        auto.SendKeys('!{F4}')
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
