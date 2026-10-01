"""0.5.3: the Win32 driver's message protocol, against a model of user32 (runs on any OS).

It checks what the driver sends, not Windows itself: menu item lookup by text (and the positional fallback only for
the known 5.8.2 executable), WM_COMMAND/BN_CLICKED to the button's PARENT, TDM_CLICK_BUTTON for task dialogs,
WM_COMMAND with the button ID for classic message boxes, wx dialog buttons by their wxID_* control IDs, LB_SETCURSEL +
numeric-keypad '+' (idempotent "set checked") for the cloth list, exact CB_FINDSTRINGEXACT + CBN_SELCHANGE for the
script box, WM_GETTEXT through SendMessageTimeoutW, and that no input is simulated (no SendInput / SetCursorPos /
keybd_event / mouse_event exist in the driver)."""
import ctypes
import sys
import unittest
from pathlib import Path
from unittest import mock

from cbbe3bbb.conversion import win32_ui
from cbbe3bbb.conversion.os_control import IDNO, IDOK, IDYES, Win

WM_GETTEXT, WM_GETTEXTLENGTH, WM_COMMAND, WM_CLOSE, WM_KEYDOWN, WM_KEYUP = 0x0D, 0x0E, 0x111, 0x10, 0x100, 0x101


class F:
    def __init__(self, fn):
        self.fn, self.argtypes, self.restype = fn, None, None

    def __call__(self, *a):
        return self.fn(*a)


class W:
    def __init__(self, h, cls, title="", pid=77, parent=0, cid=0, style=0, visible=True, enabled=True, text=None):
        self.h, self.cls, self.title, self.pid, self.parent, self.cid, self.style = h, cls, title, pid, parent, cid, style
        self.visible, self.enabled = visible, enabled
        self.text = title if text is None else text
        self.items, self.cursel, self.count = [], -1, 0


class FakeUser32:
    def __init__(self):
        self.w: dict[int, W] = {}
        self.posted: list[tuple] = []
        self.menus = {}  # hmenu -> list of (text, id or None, submenu)
        self.window_menu = {}

    def add(self, *a, **k):
        x = W(*a, **k)
        self.w[x.h] = x
        return x

    def __getattr__(self, name):
        impl = getattr(self, "_" + name, None)
        if impl is None:
            raise AttributeError(name)
        f = F(impl)
        self.__dict__[name] = f
        return f

    # ---- windows
    def _EnumWindows(self, cb, lp):
        for h, x in list(self.w.items()):
            if not x.parent:
                cb(h, lp)
        return 1

    def _EnumChildWindows(self, parent, cb, lp):
        def walk(p):
            for h, x in list(self.w.items()):
                if x.parent == p:
                    cb(h, lp)
                    walk(h)
        walk(parent)
        return 1

    def _GetWindowThreadProcessId(self, h, pp):
        pp._obj.value = self.w[h].pid
        return 1

    def _IsWindowVisible(self, h):
        return int(self.w[h].visible)

    def _IsWindowEnabled(self, h):
        return int(self.w[h].enabled)

    def _IsWindow(self, h):
        return int(h in self.w)

    def _GetClassNameW(self, h, buf, n):
        buf.value = self.w[h].cls
        return len(buf.value)

    def _GetWindowTextLengthW(self, h):
        return len(self.w[h].title)

    def _GetWindowTextW(self, h, buf, n):
        buf.value = self.w[h].title[: n - 1]
        return len(buf.value)

    def _GetWindowLongW(self, h, idx):
        return self.w[h].style

    def _GetDlgCtrlID(self, h):
        return self.w[h].cid

    def _GetParent(self, h):
        return self.w[h].parent or None

    def _GetWindowRect(self, h, r):
        return 1

    def _WaitForInputIdle(self, h, ms):
        return 0

    # ---- messages
    def _SendMessageTimeoutW(self, h, msg, wp, lp, flags, timeout, res):
        x = self.w.get(h)
        if x is None:
            return 0
        out = 0
        if msg == WM_GETTEXTLENGTH:
            out = len(x.text)
        elif msg == WM_GETTEXT:
            s = x.text[: wp - 1] + "\0"
            ctypes.memmove(lp, ctypes.create_unicode_buffer(s), len(s) * ctypes.sizeof(ctypes.c_wchar))
            out = len(s) - 1
        elif msg == win32_ui.LB_GETCOUNT:
            out = len(x.items)
        elif msg == win32_ui.LB_GETTEXTLEN:
            out = len(x.items[wp])
        elif msg == win32_ui.LB_GETTEXT:
            s = x.items[wp] + "\0"
            ctypes.memmove(lp, ctypes.create_unicode_buffer(s), len(s) * ctypes.sizeof(ctypes.c_wchar))
            out = len(s) - 1
        elif msg == win32_ui.LB_SETCURSEL:
            x.cursel = wp
            out = wp
        elif msg == win32_ui.LB_GETCURSEL:
            out = x.cursel
        elif msg == win32_ui.LVM_GETITEMCOUNT:
            out = x.count
        elif msg == win32_ui.CB_FINDSTRINGEXACT:
            s = ctypes.wstring_at(lp)
            out = x.items.index(s) if s in x.items else ctypes.c_size_t(-1).value
        elif msg == win32_ui.CB_SETCURSEL:
            x.text = x.items[wp]
            out = wp
        res._obj.value = out
        return 1

    def _PostMessageW(self, h, msg, wp, lp):
        self.posted.append((h, msg, wp, lp))
        return 1

    # ---- menus
    def _GetMenu(self, h):
        return self.window_menu.get(h)

    def _GetMenuItemCount(self, m):
        return len(self.menus[m])

    def _GetSubMenu(self, m, i):
        return self.menus[m][i][2]

    def _GetMenuItemID(self, m, i):
        it = self.menus[m][i]
        return 0xFFFFFFFF if it[1] is None else it[1]

    def _GetMenuStringW(self, m, i, buf, n, flags):
        buf.value = self.menus[m][i][0]
        return len(buf.value)


class Win32DriverTests(unittest.TestCase):
    def setUp(self):
        self.u = FakeUser32()
        patches = [mock.patch.object(sys, "platform", "win32")]
        if not hasattr(ctypes, "WinDLL"):
            patches.append(mock.patch.object(ctypes, "WinDLL", create=True))
        if not hasattr(ctypes, "WINFUNCTYPE"):
            patches.append(mock.patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        ctypes.WinDLL = lambda name, use_last_error=False: self.u if name == "user32" else FakeUser32()
        self.d = win32_ui.Win32Driver(exe_sha256="53cec5952a320d2d5ec9582fe111ccdb735304d2e926e272cb65440f4cc4218c")

    def test_windows_texts_and_lists(self):
        u, d = self.u, self.d
        u.add(10, "wxWindowNR", "Outfit Studio")
        u.add(11, "wxWindowNR", "other", pid=5)
        a = u.add(20, "wxWindowNR", "Automation Script")
        u.add(21, "ComboBox", parent=20, cid=301, style=0x2, text="cbbe3bbb_batch_X_1")
        lv = u.add(22, "SysListView32", parent=20)
        lv.count = 7
        u.add(23, "Button", parent=20, cid=305, text="&Execute")
        tops = d.top_windows(77)
        self.assertEqual(sorted(w.title for w in tops), ["Automation Script", "Outfit Studio"])
        kids = d.children(a.h)
        self.assertEqual([k.cls for k in kids], ["ComboBox", "SysListView32", "Button"])
        self.assertEqual(kids[0].style & 3, 2)
        self.assertEqual(d.text(21), "cbbe3bbb_batch_X_1")
        self.assertEqual(d.listview_count(22), 7)
        lb = u.add(30, "ListBox", parent=20)
        lb.items = [r"T:\w\Data\Tools\BodySlide\ShapeData\VtawWardrobe10\LatexSuitTop.nif"]
        self.assertEqual(d.listbox_items(30), lb.items)

    def test_menu_command_by_text_and_known_layout_fallback(self):
        u, d = self.u, self.d
        u.add(10, "wxWindowNR", "Outfit Studio")
        u.window_menu[10] = 1
        items = [(f"Item {i}", 100 + i, None) for i in range(10)] + [("Open &Automation...\tCtrl+Shift+A", 0xFFFF8123, None)]
        u.menus = {1: [("&File", None, 2)], 2: items}
        mid = d.menu_command_id(10, "Open Automation")
        self.assertEqual(mid, 0x8123)  # the low word, as WM_COMMAND carries it (wx sign-extends it again)
        self.assertTrue(d.post_command(10, mid))
        self.assertEqual(u.posted[-1], (10, WM_COMMAND, 0x8123, 0))
        # menu text unreadable (owner-drawn): only the known 5.8.2 layout (File, 11th entry) is used
        u.menus = {1: [("", None, 2)], 2: [("", 100 + i, None) for i in range(10)] + [("", 0x9001, None)]}
        self.assertEqual(d.menu_command_id(10, "Open Automation"), 0x9001)
        d.exe_sha256 = "unknown"
        self.assertIsNone(d.menu_command_id(10, "Open Automation"))

    def test_buttons_dialogs_and_cloth_list(self):
        u, d = self.u, self.d
        u.add(20, "wxWindowNR", "Automation Script")
        u.add(24, "wxWindowNR", parent=20)
        btn = u.add(25, "Button", parent=24, cid=0x8200, text="Execute")
        self.assertTrue(d.click_button(Win(hwnd=25, cls="Button", ctrl_id=0x8200, parent=24)))
        self.assertEqual(u.posted[-1], (24, WM_COMMAND, 0x8200, 25))  # BN_CLICKED (0) to the PARENT
        # the task dialog exactly as the real 5.8.2 run captured it (batch 20261001-002408, session 1/2):
        # #32770 "Confirm Execution" > DirectUIHWND > CtrlNotifySink > Button, BOTH buttons with control ID 0
        u.add(40, "#32770", "Confirm Execution")
        u.add(41, "DirectUIHWND", parent=40)
        u.add(44, "CtrlNotifySink", parent=41)
        u.add(45, "CtrlNotifySink", parent=41)
        u.add(42, "Button", parent=44, cid=0, text="&Yes")
        u.add(43, "Button", parent=45, cid=0, text="&No")
        btns = d.dialog_buttons(40)
        self.assertEqual([(b["id"], b["text"]) for b in btns], [(0, "Yes"), (0, "No")])  # 0.5.3 keyed them by ID → {0: 'No'}
        yes = next(b for b in btns if b["text"] == "Yes")
        self.assertTrue(d.press_dialog_button(Win(hwnd=40, cls="#32770", title="Confirm Execution"), yes, IDYES))
        self.assertEqual(u.posted[-1], (40, win32_ui.TDM_CLICK_BUTTON, IDYES, 0))
        n = len(u.posted)  # a button of another window is refused
        self.assertFalse(d.press_dialog_button(Win(hwnd=40, cls="#32770"), {"hwnd": 25, "id": 0, "text": "Yes"}, IDYES))
        self.assertEqual(len(u.posted), n)
        # a classic message box: WM_COMMAND with the button's own ID
        u.add(50, "#32770", "Old style")
        u.add(51, "Button", parent=50, cid=IDOK, text="OK")
        ok = d.dialog_buttons(50)[0]
        self.assertTrue(d.press_dialog_button(Win(hwnd=50, cls="#32770"), ok, IDOK))
        self.assertEqual(u.posted[-1], (50, WM_COMMAND, IDOK, 51))
        # the wx 'Choose cloth data' dialog: its OK button is wxID_OK (5100)
        u.add(60, "wxWindowNR", "Choose cloth data")
        lb = u.add(61, "ListBox", parent=60)
        lb.items = ["a.nif", "b.nif"]
        u.add(62, "Button", parent=60, cid=5100, text="OK")
        self.assertTrue(d.listbox_check(61, 1))
        self.assertEqual(lb.cursel, 1)
        self.assertEqual([m[1:3] for m in u.posted[-2:]], [(WM_KEYDOWN, win32_ui.VK_ADD), (WM_KEYUP, win32_ui.VK_ADD)])
        self.assertFalse(d.press_dialog_button(Win(hwnd=60, cls="wxWindowNR"), d.dialog_buttons(60)[0], IDOK))  # not native
        self.assertTrue(d.click_button(Win(hwnd=62, cls="Button", ctrl_id=5100, parent=60)))
        self.assertEqual(u.posted[-1], (60, WM_COMMAND, 5100, 62))
        self.assertTrue(d.close_window(60))
        self.assertEqual(u.posted[-1][:2], (60, WM_CLOSE))

    def test_script_box_exact_selection(self):
        u, d = self.u, self.d
        u.add(20, "wxWindowNR", "Automation Script")
        cb = u.add(21, "ComboBox", parent=20, cid=0x8300, style=0x2, text="")
        cb.items = ["<New>", "cbbe3bbb_batch_X_1", "cbbe3bbb_batch_X_2"]
        self.assertFalse(d.combo_select(Win(hwnd=21, cls="ComboBox", ctrl_id=0x8300, parent=20), "cbbe3bbb_batch_X_9"))
        self.assertTrue(d.combo_select(Win(hwnd=21, cls="ComboBox", ctrl_id=0x8300, parent=20), "cbbe3bbb_batch_X_2"))
        self.assertEqual(cb.text, "cbbe3bbb_batch_X_2")
        self.assertEqual(u.posted[-1], (20, WM_COMMAND, (1 << 16) | 0x8300, 21))  # CBN_SELCHANGE to the parent

    def test_no_simulated_input(self):
        src = Path(win32_ui.__file__).read_text(encoding="utf-8")
        for api in ("SendInput", "SetCursorPos", "keybd_event", "mouse_event", "SetForegroundWindow"):
            self.assertNotIn(api + "(", src)


if __name__ == "__main__":
    unittest.main()
