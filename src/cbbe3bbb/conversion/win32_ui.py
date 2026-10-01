"""0.5.3: the Windows side of the unattended Outfit Studio control (:mod:`os_control`).

Only plain Win32 window messages to Outfit Studio's own native controls are used (Outfit Studio 5.8.2 is built with
wxWidgets 3.3 for MSW, whose buttons, combo boxes, list boxes, list views, rich edit controls and menus are native
Windows controls):

* reading: window titles/classes/styles/control IDs, ``WM_GETTEXT`` (marshalled by Windows across processes),
  ``LB_GETCOUNT``/``LB_GETTEXT``, ``LVM_GETITEMCOUNT``, the native menu (``GetMenuStringW``/``GetMenuItemID``);
* acting: ``WM_COMMAND`` with the menu item's ID to the main window; ``WM_COMMAND``/``BN_CLICKED`` of a button to its
  parent (what the button itself sends when clicked); ``TDM_CLICK_BUTTON`` for Windows task dialogs (wxMessageBox);
  ``LB_SETCURSEL`` + the numeric-keypad ``+`` key message to a wx check list box (wxCheckListBox::OnKeyDown: "set
  checked", idempotent); ``WM_CLOSE``.

No simulated keyboard or mouse input (``SendInput``), no screen coordinates, no focus or foreground requirement, and
therefore independent of DPI, resolution and window position. Reads use ``SendMessageTimeoutW`` so a busy Outfit
Studio (a long CopyBoneWeights step) never blocks the converter. Every call works only on windows of the Outfit Studio
process the converter started.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from .os_control import UIDriver, Win

__all__ = ["Win32Driver", "available", "KNOWN_MENU_LAYOUT"]

# Outfit Studio 5.8.2 (the release copied into the converter's runtime): File menu, 11th entry = "Open Automation..."
# (res/xrc/OutfitStudio.xrc). Used only if the menu text cannot be read at all, and the Automation window must then
# still appear and show the exact script, or nothing is executed.
KNOWN_MENU_LAYOUT = {"53cec5952a320d2d5ec9582fe111ccdb735304d2e926e272cb65440f4cc4218c": (0, 10)}

WM_GETTEXT, WM_GETTEXTLENGTH, WM_CLOSE, WM_COMMAND = 0x000D, 0x000E, 0x0010, 0x0111
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
BN_CLICKED, CBN_SELCHANGE = 0, 1
LB_SETCURSEL, LB_GETCURSEL, LB_GETTEXT, LB_GETTEXTLEN, LB_GETCOUNT = 0x0186, 0x0188, 0x0189, 0x018A, 0x018B
CB_SETCURSEL, CB_FINDSTRINGEXACT = 0x014E, 0x0158
LVM_GETITEMCOUNT = 0x1004
TDM_CLICK_BUTTON = 0x0400 + 102
VK_ADD = 0x6B
GWL_STYLE = -16
SMTO_ABORTIFHUNG = 0x0002
MF_BYPOSITION = 0x0400


def available() -> bool:
    return sys.platform == "win32"


def _makewparam(lo: int, hi: int) -> int:
    return ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)


class Win32Driver(UIDriver):
    name = "win32"

    def __init__(self, exe_sha256: str = ""):
        if not available():
            raise RuntimeError("the Windows UI driver needs Windows")
        import ctypes
        from ctypes import wintypes as wt

        self.ct, self.wt = ctypes, wt
        self.user32 = u = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.exe_sha256 = exe_sha256
        self.WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        u.EnumWindows.argtypes = [self.WNDENUMPROC, wt.LPARAM]
        u.EnumChildWindows.argtypes = [wt.HWND, self.WNDENUMPROC, wt.LPARAM]
        u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u.GetWindowThreadProcessId.restype = wt.DWORD
        for f in ("IsWindowVisible", "IsWindowEnabled", "IsWindow"):
            getattr(u, f).argtypes = [wt.HWND]
            getattr(u, f).restype = wt.BOOL
        u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u.GetWindowTextLengthW.argtypes = [wt.HWND]
        u.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.GetDlgCtrlID.argtypes = [wt.HWND]
        u.GetParent.argtypes = [wt.HWND]
        u.GetParent.restype = wt.HWND
        u.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT,
                                          ctypes.POINTER(ctypes.c_size_t)]
        u.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.PostMessageW.restype = wt.BOOL
        u.GetMenu.argtypes = [wt.HWND]
        u.GetMenu.restype = wt.HMENU
        u.GetSubMenu.argtypes = [wt.HMENU, ctypes.c_int]
        u.GetSubMenu.restype = wt.HMENU
        u.GetMenuItemCount.argtypes = [wt.HMENU]
        u.GetMenuItemID.argtypes = [wt.HMENU, ctypes.c_int]
        u.GetMenuItemID.restype = wt.UINT
        u.GetMenuStringW.argtypes = [wt.HMENU, wt.UINT, wt.LPWSTR, ctypes.c_int, wt.UINT]
        u.WaitForInputIdle.argtypes = [wt.HANDLE, wt.DWORD]
        u.WaitForInputIdle.restype = wt.DWORD
        u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]

    # ----------------------------------------------------------------- process
    def launch(self, exe: Path, cwd: Path):
        return subprocess.Popen([str(exe)], cwd=str(cwd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def poll(self, proc) -> Optional[int]:
        return proc.poll()

    def kill(self, proc) -> None:
        try:
            proc.kill()
            proc.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def wait_idle(self, proc, timeout_ms: int) -> bool:
        h = getattr(proc, "_handle", None)
        if h is None:
            return True
        return self.user32.WaitForInputIdle(int(h), int(timeout_ms)) == 0

    # ----------------------------------------------------------------- windows
    def _class(self, hwnd: int) -> str:
        buf = self.ct.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def _title(self, hwnd: int) -> str:
        n = self.user32.GetWindowTextLengthW(hwnd)
        buf = self.ct.create_unicode_buffer(max(n, 0) + 1)
        self.user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _win(self, hwnd: int, title: Optional[str] = None) -> Win:
        u = self.user32
        return Win(hwnd=int(hwnd), title=self._title(hwnd) if title is None else title, cls=self._class(hwnd),
                   visible=bool(u.IsWindowVisible(hwnd)), enabled=bool(u.IsWindowEnabled(hwnd)),
                   ctrl_id=int(u.GetDlgCtrlID(hwnd)) & 0xFFFF, style=int(u.GetWindowLongW(hwnd, GWL_STYLE)) & 0xFFFFFFFF,
                   parent=int(u.GetParent(hwnd) or 0))

    def top_windows(self, pid: int) -> list[Win]:
        out: list[int] = []
        wt = self.wt

        def cb(hwnd, _):
            p = wt.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, self.ct.byref(p))
            if p.value == pid:
                out.append(hwnd)
            return True

        self.user32.EnumWindows(self.WNDENUMPROC(cb), 0)
        return [self._win(h) for h in out if self.user32.IsWindow(h)]

    def children(self, hwnd: int) -> list[Win]:
        out: list[int] = []

        def cb(h, _):
            out.append(h)
            return True

        self.user32.EnumChildWindows(hwnd, self.WNDENUMPROC(cb), 0)
        return [self._win(h, title="") for h in out]

    def _send(self, hwnd: int, msg: int, wparam: int = 0, lparam: int = 0, timeout_ms: int = 2000) -> Optional[int]:
        res = self.ct.c_size_t()
        ok = self.user32.SendMessageTimeoutW(hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, timeout_ms, self.ct.byref(res))
        return int(res.value) if ok else None

    def text(self, hwnd: int) -> Optional[str]:
        if not hwnd or not self.user32.IsWindow(hwnd):
            return None
        n = self._send(hwnd, WM_GETTEXTLENGTH)
        if n is None:
            return None
        buf = self.ct.create_unicode_buffer(n + 2)
        got = self._send(hwnd, WM_GETTEXT, n + 1, self.ct.cast(buf, self.ct.c_void_p).value)
        return None if got is None else buf.value

    # ----------------------------------------------------------------- menus / commands
    def menu_command_id(self, hwnd: int, label_prefix: str) -> Optional[int]:
        u = self.user32
        hm = u.GetMenu(hwnd)
        if not hm:
            return None
        any_text = False

        def walk(menu, depth=0):
            nonlocal any_text
            for i in range(max(u.GetMenuItemCount(menu), 0)):
                buf = self.ct.create_unicode_buffer(512)
                u.GetMenuStringW(menu, i, buf, 512, MF_BYPOSITION)
                txt = buf.value.replace("&", "")
                any_text = any_text or bool(txt)
                mid = u.GetMenuItemID(menu, i)
                if mid != 0xFFFFFFFF and txt.startswith(label_prefix):
                    return mid & 0xFFFF
                sub = u.GetSubMenu(menu, i)
                if sub and depth < 3:
                    r = walk(sub, depth + 1)
                    if r is not None:
                        return r
            return None

        found = walk(hm)
        if found is not None:
            return found
        pos = KNOWN_MENU_LAYOUT.get(self.exe_sha256)  # the item's text is not readable (e.g. owner-drawn)
        if pos is None:
            return None
        sub = u.GetSubMenu(hm, pos[0])
        mid = u.GetMenuItemID(sub, pos[1]) if sub else 0xFFFFFFFF
        return None if mid == 0xFFFFFFFF else mid & 0xFFFF

    def post_command(self, hwnd: int, cmd_id: int) -> bool:
        return bool(self.user32.PostMessageW(hwnd, WM_COMMAND, _makewparam(cmd_id, 0), 0))

    def click_button(self, btn: Win) -> bool:
        parent = btn.parent or int(self.user32.GetParent(btn.hwnd) or 0)
        if not parent:
            return False
        return bool(self.user32.PostMessageW(parent, WM_COMMAND, _makewparam(btn.ctrl_id, BN_CLICKED), btn.hwnd))

    # ----------------------------------------------------------------- lists
    def listview_count(self, hwnd: int) -> Optional[int]:
        return self._send(hwnd, LVM_GETITEMCOUNT)

    def listbox_items(self, hwnd: int) -> Optional[list[str]]:
        n = self._send(hwnd, LB_GETCOUNT)
        if n is None or n > 10000:
            return None
        items = []
        for i in range(n):
            ln = self._send(hwnd, LB_GETTEXTLEN, i)
            if ln is None or ln > 32768:
                return None
            buf = self.ct.create_unicode_buffer(ln + 2)
            got = self._send(hwnd, LB_GETTEXT, i, self.ct.cast(buf, self.ct.c_void_p).value)
            if got is None:
                return None
            items.append(buf.value)
        return items

    def listbox_check(self, hwnd: int, index: int) -> bool:
        if self._send(hwnd, LB_SETCURSEL, index) is None or self._send(hwnd, LB_GETCURSEL) != index:
            return False
        lp_down = 1 | (0x4E << 16)  # repeat 1, scan code of numeric keypad '+'
        lp_up = lp_down | (1 << 30) | (1 << 31)
        return bool(self.user32.PostMessageW(hwnd, WM_KEYDOWN, VK_ADD, lp_down)
                    and self.user32.PostMessageW(hwnd, WM_KEYUP, VK_ADD, lp_up))

    def combo_select(self, combo: Win, text: str) -> bool:
        buf = self.ct.create_unicode_buffer(text)
        minus_one = self.ct.c_size_t(-1).value  # CB_ERR as an unsigned LRESULT / "search from the start" as WPARAM
        idx = self._send(combo.hwnd, CB_FINDSTRINGEXACT, minus_one, self.ct.cast(buf, self.ct.c_void_p).value)
        if idx is None or idx == minus_one:
            return False
        if self._send(combo.hwnd, CB_SETCURSEL, idx) is None:
            return False
        parent = combo.parent or int(self.user32.GetParent(combo.hwnd) or 0)
        return bool(parent and self.user32.PostMessageW(parent, WM_COMMAND, _makewparam(combo.ctrl_id, CBN_SELCHANGE),
                                                        combo.hwnd))

    # ----------------------------------------------------------------- dialogs
    def _is_task_dialog(self, hwnd: int) -> bool:
        return self._class(hwnd) == "#32770" and any(k.cls == "DirectUIHWND" for k in self.children(hwnd))

    def dialog_buttons(self, hwnd: int) -> list[dict]:
        # all of them, in window order — task-dialog buttons all have control ID 0 (real 5.8.2 run, 2026-10-01)
        return [{"hwnd": k.hwnd, "id": k.ctrl_id, "text": (self.text(k.hwnd) or "").replace("&", ""), "enabled": k.enabled}
                for k in self.children(hwnd) if k.cls.lower() == "button" and k.visible]

    def press_dialog_button(self, dlg: Win, button: dict, task_id: int) -> bool:
        u = self.user32
        kids = self.children(dlg.hwnd)
        if not any(k.hwnd == button.get("hwnd") for k in kids):
            return False  # not a button of this dialog
        if self._class(dlg.hwnd) != "#32770":
            return False
        if any(k.cls == "DirectUIHWND" for k in kids):  # a task dialog (wxMessageBox on Windows)
            return bool(u.PostMessageW(dlg.hwnd, TDM_CLICK_BUTTON, task_id, 0))
        bid = int(button.get("id") or 0)  # a classic message box: its buttons carry IDOK/IDYES/…
        return bool(bid and u.PostMessageW(dlg.hwnd, WM_COMMAND, _makewparam(bid, BN_CLICKED), button["hwnd"]))

    def close_window(self, hwnd: int) -> bool:
        return bool(self.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))

    def screenshot(self, hwnd: int, path: Path) -> bool:
        try:
            from PIL import ImageGrab  # optional
        except ImportError:
            return False
        r = self.wt.RECT()
        if not self.user32.GetWindowRect(hwnd, self.ct.byref(r)):
            return False
        try:
            ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True).save(path)
            return True
        except Exception:  # noqa: BLE001 - best effort diagnostics
            return False
