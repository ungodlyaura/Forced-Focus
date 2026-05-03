# ForceFocus (Anki Add-on)

ForceFocus restricts access to other applications until you complete Anki reviews. Time is earned by reviewing cards and consumed in real time. When time runs out, focus is forced back to Anki until enough time is regained.

---

## Core Behavior

* You start **locked**.
* Reviewing cards grants **usable time**.
* While time remains, you can use other applications.
* When time reaches **0 or below**, focus is forced back to Anki.
* You must reach **`min_unlock_time`** again to regain freedom.

---

## List of OS/WM compatibility
Windows (Works)
Uses Win32 APIs (pywin32, psutil, plyer) to detect active windows and force focus.
Hyprland (Wayland) (Works)
Uses hyprctl for active window detection and focus control.
Sway (Wayland) (Untested)
Uses swaymsg IPC to traverse the window tree and control focus.
KDE Plasma (Doesn't work)

---

## Configuration

### Time & Unlocking

* **`min_unlock_time`**
  Minimum seconds required before you are allowed to leave Anki after being locked.

* **`max_time`**
  Maximum amount of time that can be accumulated. `0` = no cap.

* **`allow_negative_time`**
  If enabled, time can go below zero (debt). If disabled, time clamps at zero.

---

### Reward System

* **`reward_ratings`**
  Card ratings that grant time (Anki ease buttons).
  Default: `[2, 3, 4]` (Good/Easy-type answers).

* **`mode`**
  Time gain mode:

  * `"per_card"` → fixed reward per card
  * `"per_second"` → based on time spent answering

* **`time_per_card`**
  Seconds gained per card when using `"per_card"` mode.

* **`time_per_second`**
  Multiplier for time spent reviewing when using `"per_second"` mode.

---

### Activity Handling

* **`auto_pause`**
  Prevents time drain while actively reviewing cards.

* **`recent_activity_window`**
  Time window (seconds) after a review where activity is considered “active”.

---

### Daily Limits

* **`daily_quota`**
  Number of cards required to reach quota.

* **`reset_time`**
  Time of day when daily stats reset (24h format, e.g. `"04:00"`).

---

### Undo Behavior

* **`subtract_on_undo`**
  Removes previously gained time if a review is undone.

---

### Notifications

* **`notifications_enabled`**
  Enables warning notifications.

* **`notification_start_time`**
  Remaining time (seconds) when warnings begin.

* **`notification_interval`**
  Time between repeated warnings (seconds).

* **`notification_duration`**
  Duration notifications stay visible (seconds).

---

### App Exemptions

* **`exempt_identifiers`**
  List of app identifiers that are allowed even while locked.
  Matches window class/title/process name substrings.

---

### Windows-Specific Behavior

* **`WINDOWS_UNMINIMIZE`**
  Restores Anki if minimized when forcing focus.

* **`WINDOWS_MAXIMIZE`**
  Maximizes Anki window when focused.

* **`WINDOWS_FULLSCREEN`**
  Forces borderless fullscreen mode on Anki.

---

## Notes

* Enforcement runs continuously; while locked, focus will repeatedly return to Anki.
* Incorrect configuration of `min_unlock_time` will significantly affect usability.
* Platform-specific window handling may behave differently depending on the window manager or OS limitations.
