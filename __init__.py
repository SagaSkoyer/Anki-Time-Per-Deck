"""Time Per Deck.

Switches targeted decks from due-count based studying to time-based studying.
A target is set per deck from the deck list (the "Minutes per deck" button),
and configured deck rows replace the New/Learn/Due counts with a progress bar
for today's study target.

For a deck with target T minutes, the time spent answering its cards (and its
sub-decks') today decides one of three states:

  * spent < T          -> UNDER: if the deck runs out of available work, pull
    new cards beyond the deck's normal daily new limit.
  * spent >= T         -> DONE: bury remaining new cards today; due reviews
    remain available until the grace cutoff.

The state is recomputed live as you study, so crossing a boundary takes effect
on the very next card.
"""
from __future__ import annotations

import json
import html
import math
import os
import re
import time

from anki.scheduler.v3 import QueuedCards
from anki.scheduler.v3 import Scheduler as V3Scheduler
from anki.utils import ids2str
from aqt import gui_hooks, mw
from aqt.deckbrowser import DeckBrowser, DeckBrowserBottomBar
from aqt.qt import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTimer,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import askUser, tooltip

PYCMD_SETTINGS = "tpd_open_settings"
BAR_ID = "_timePerDeckProgressWidget"
ADDON_DIR = os.path.dirname(__file__)
DATA_FILE = os.path.join(ADDON_DIR, "user_files", "fail_counts.json")

# Light purple used for the column header and values.
PURPLE = "#b39ddb"
PROGRESS_ACTIVE = "#4caf50"
PROGRESS_COMPLETE = "#FFD700"
PROGRESS_OVERTIME = PROGRESS_COMPLETE
PROGRESS_TEXT = "#888888"
PROGRESS_TRACK = "rgba(136,136,136,.25)"
ASCENDING_RETRIEVABILITY_LABEL = "Ascending retrievability"
SORT_OPTION_WARNING = (
    "This deck preset should be changed to ascending retrievability for "
    "the add-on to properly function across days."
)
SETTINGS_DECK_COLUMN_WIDTH = 310
REVIEW_ORDER_LABELS = {
    0: "Due date",
    1: "Due date, then deck",
    2: "Deck, then due date",
    3: "Ascending intervals",
    4: "Descending intervals",
    5: "Ascending ease",
    6: "Descending ease",
    7: ASCENDING_RETRIEVABILITY_LABEL,
    8: "Random",
    9: "Order added",
    10: "Reverse order added",
    11: "Descending retrievability",
    12: "Relative overdueness",
}
SPINBOX_NO_ARROWS = """
QSpinBox::up-button, QSpinBox::down-button {
    width: 0px;
    border: none;
}
QSpinBox::up-arrow, QSpinBox::down-arrow {
    width: 0px;
    height: 0px;
}
"""

# Time budget behavior.
DEFAULT_OVER_LIMIT_GRACE_PERCENT = 20
DEFAULT_MAX_ANSWER_SECONDS = 60
EXTEND_BATCH = 1
DEFAULT_BACKLOG_FAIL_LIMIT = 5

# Card queue ids (see anki.consts).
_QUEUE_NEW = 0
_QUEUE_LEARN = 1
_QUEUE_REV = 2
_QUEUE_DAY_LEARN = 3

# How many cards to fetch from the backend when filtering new cards out of the
# queue for blocked decks. The backend returns cards in scheduling order, so we
# ask for plenty and drop the blocked-deck new cards.
_INTERNAL_FETCH = 1000

# State labels.
UNDER = "under"
DONE = "done"
OVER = "over"

# Set of deck ids (target decks + their children) whose NEW cards must be
# suppressed in the reviewer. Recomputed on every answer / state change; None
# means "stale, recompute on next use".
_blocked: set[int] | None = None

# Reentrancy guard for the deck-browser apply pass.
_applying = False

_orig_get_queued_cards = None

# How long after an over-limit bury we re-fire the same bury, in milliseconds.
# The reviewer can pre-fetch cards before the target is crossed, so a card can
# slip past the first bury; re-firing on the same target deck a few seconds
# later sweeps up anything that leaked through.
OVER_REFIRE_MS = 10000

# Deferred re-fire of the over-limit bury. `_over_refire_timer` is a lazily
# created single-shot QTimer; `_over_refire_dids` holds the target decks queued
# for the next re-fire.
_over_refire_timer: "QTimer | None" = None
_over_refire_dids: set[int] = set()


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event) -> None:
        event.ignore()


def _section_divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def _vertical_divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


# Review progress widget ------------------------------------------------------

def _progress_bar_style(color: str) -> str:
    return """
QProgressBar {
    background-color: rgba(136,136,136,.25);
    border: none;
    border-radius: 5px;
    color: #888888;
    font-size: 10px;
    min-height: 12px;
    max-height: 12px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: %s;
    border-radius: 5px;
}
""" % color


def _make_progress_bar() -> QProgressBar:
    bar = QProgressBar()
    bar.setRange(0, 1000)
    bar.setTextVisible(False)
    bar.setMinimumWidth(180)
    return bar


def _progress_widget() -> QWidget:
    existing = getattr(mw, BAR_ID, None)
    if existing is not None:
        return existing

    w = QWidget()
    grid = QGridLayout(w)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(8)
    grid.setVerticalSpacing(2)
    grid.setColumnStretch(1, 1)

    def label(text: str) -> QLabel:
        out = QLabel(text)
        out.setStyleSheet("QLabel { color: #888888; }")
        out.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        out.setMinimumWidth(112)
        return out

    w.current_name = label("")
    w.overall_name = label("")
    w.current_bar = _make_progress_bar()
    w.overall_bar = _make_progress_bar()
    w.current_count = label("")
    w.overall_count = label("")
    w.current_count.setAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )
    w.overall_count.setAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )
    w.current_count.setMinimumWidth(104)
    w.overall_count.setMinimumWidth(104)
    w.current_tdid = None
    w.current_base_spent = 0.0
    w.current_target = 0
    w.overall_base_spent = 0.0
    w.overall_target = 0
    w.overall_live_room = 0.0
    w.overall_remaining_rows = []
    w.live_started_at = None
    w.live_max_seconds = DEFAULT_MAX_ANSWER_SECONDS

    grid.addWidget(w.current_name, 0, 0)
    grid.addWidget(w.current_bar, 0, 1)
    grid.addWidget(w.current_count, 0, 2)
    grid.addWidget(w.overall_name, 1, 0)
    grid.addWidget(w.overall_bar, 1, 1)
    grid.addWidget(w.overall_count, 1, 2)

    mw.statusBar().addPermanentWidget(w, 1)
    setattr(mw, BAR_ID, w)
    w.live_timer = QTimer(w)
    w.live_timer.setInterval(1000)
    w.live_timer.timeout.connect(lambda: _tick_progress_widget(w))
    return w


def _deck_name(did: int) -> str:
    try:
        return mw.col.decks.name(did)
    except Exception:
        deck = mw.col.decks.get(did)
        if deck:
            return str(deck.get("name", did))
        return str(did)


def _configured_target_decks() -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for sdid, minutes in _targets().items():
        try:
            did = int(sdid)
            target = int(minutes)
        except (TypeError, ValueError):
            continue
        if target > 0:
            out.append((did, target))
    return out


def _current_target_deck() -> int | None:
    reviewer = getattr(mw, "reviewer", None)
    card = getattr(reviewer, "card", None)
    if card is not None:
        return _target_deck_for(card.did)
    try:
        return _target_deck_for(mw.col.decks.get_current_id())
    except Exception:
        return None


def _current_review_card():
    reviewer = getattr(mw, "reviewer", None)
    return getattr(reviewer, "card", None)


def _config_value(cfg, key: str):
    if cfg is None:
        return None
    if isinstance(cfg, dict):
        return cfg.get(key)
    return getattr(cfg, key, None)


def _nested_config_value(cfg, *path: str):
    current = cfg
    for key in path:
        current = _config_value(current, key)
        if current is None:
            return None
    return current


def _humanize_sort_value(value) -> str:
    enum_name = getattr(value, "name", None)
    if enum_name is not None:
        value = enum_name

    if isinstance(value, int):
        return REVIEW_ORDER_LABELS.get(value, "Unknown (%d)" % value)

    text = str(value).strip()
    if not text:
        return "Unknown"
    try:
        return REVIEW_ORDER_LABELS.get(int(text), "Unknown (%s)" % text)
    except ValueError:
        pass

    normalized = re.sub(r"[_\-\s]+", " ", text).strip().lower()
    if (
        "retrievability" in normalized
        and "ascending" in normalized
        and "descending" not in normalized
    ):
        return ASCENDING_RETRIEVABILITY_LABEL

    text = re.sub(r"^REVIEW_CARD_ORDER_", "", text)
    text = re.sub(r"[_\-]+", " ", text).strip()
    return text.capitalize() if text else "Unknown"


def _is_ascending_retrievability_sort(value) -> bool:
    enum_name = getattr(value, "name", None)
    if enum_name is not None:
        value = enum_name

    if isinstance(value, int):
        return value == 7

    text = str(value).strip()
    try:
        return int(text) == 7
    except ValueError:
        pass

    normalized = re.sub(r"[_\-\s]+", " ", text).strip().lower()
    return (
        "retrievability" in normalized
        and "ascending" in normalized
        and "descending" not in normalized
    )


def _deck_sort_option_status(did: int) -> tuple[str, bool]:
    cfg = _deck_config_for_did(did)
    for path in (
        ("config", "review_order"),
        ("config", "reviewOrder"),
        ("config", "reviewCardOrder"),
        ("review_order",),
        ("reviewOrder",),
        ("reviewCardOrder",),
        ("review_card_order",),
    ):
        value = _nested_config_value(cfg, *path)
        if value is not None:
            return _humanize_sort_value(value), _is_ascending_retrievability_sort(
                value
            )
    return "Unknown", False


def _deck_config_for_did(did: int):
    decks = mw.col.decks
    for method_name in ("config_dict_for_deck_id", "confForDid"):
        method = getattr(decks, method_name, None)
        if method is None:
            continue
        try:
            cfg = method(did)
            if cfg is not None:
                return cfg
        except Exception:
            pass

    try:
        deck = decks.get(did)
    except Exception:
        deck = None
    conf_id = None
    if isinstance(deck, dict):
        conf_id = deck.get("conf")
    if conf_id is None:
        return None

    for method_name in ("get_config", "get_config_by_id"):
        method = getattr(decks, method_name, None)
        if method is None:
            continue
        try:
            cfg = method(conf_id)
            if cfg is not None:
                return cfg
        except Exception:
            pass
    return None


def _max_answer_seconds_for_did(did: int) -> int:
    cfg = _deck_config_for_did(did)
    for key in (
        "maxTaken",
        "max_taken",
        "maximum_answer_seconds",
        "maximumAnswerSeconds",
        "max_answer_secs",
    ):
        try:
            value = int(_config_value(cfg, key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return DEFAULT_MAX_ANSWER_SECONDS


def _set_progress_row(
    name: QLabel,
    name_base: str,
    bar: QProgressBar,
    count: QLabel,
    spent: float,
    target: float,
    *,
    color_override: str | None = None,
    tooltip_spent: float | None = None,
    tooltip_target: int | None = None,
    count_tooltip: str | None = None,
) -> None:
    pct = 0.0 if target <= 0 else max(0.0, min(100.0, spent / target * 100))
    color = color_override or (PROGRESS_COMPLETE if spent >= target else PROGRESS_ACTIVE)
    bar.setStyleSheet(_progress_bar_style(color))
    bar.setValue(int(round(pct * 10)))
    name.setText("%s %d%%" % (name_base, int(pct)))
    count.setText("%s left" % _format_duration_minutes(max(0.0, target - spent)))
    tip = "%s of %s studied today" % (
        _format_duration_minutes(spent if tooltip_spent is None else tooltip_spent),
        _format_duration_minutes(target if tooltip_target is None else tooltip_target),
    )
    for widget in (name, bar):
        widget.setToolTip(tip)
    count.setToolTip(count_tooltip or tip)


def _format_duration_minutes(minutes: float) -> str:
    if minutes < 1:
        return "%d sec" % int(round(minutes * 60))
    return "%d mins" % int(minutes)


def _format_duration_minutes_max(minutes: float) -> str:
    if minutes < 1:
        return "%d sec" % int(round(minutes * 60))
    return "%d mins" % int(math.ceil(minutes))


def _max_remaining_minutes(target: int, spent: float) -> float:
    return max(0.0, target + _over_limit_grace_minutes(target) - spent)


def _remaining_range_minutes(target: int, spent: float) -> tuple[float, float]:
    min_remaining = max(0.0, target - spent)
    max_remaining = _max_remaining_minutes(target, spent)
    return min_remaining, max_remaining


def _format_range_minutes(min_minutes: float, max_minutes: float) -> str:
    def rounded(minutes: float) -> int:
        if minutes <= 0:
            return 0
        return int(math.ceil(minutes))

    return "%d-%d mins" % (rounded(min_minutes), rounded(max_minutes))


def _remaining_breakout_tooltip(
    rows: list[tuple[int, int, float]],
    *,
    live_did: int | None = None,
    live_minutes: float = 0.0,
) -> str:
    total_min = 0.0
    total_max = 0.0
    lines = []

    for did, target, spent in rows:
        if live_did == did:
            spent += live_minutes
        min_remaining, max_remaining = _remaining_range_minutes(target, spent)
        total_min += min_remaining
        total_max += max_remaining
        lines.append(
            "%s = %s" % (
                html.escape(_deck_name(did)),
                _format_range_minutes(min_remaining, max_remaining),
            )
        )

    total_line = "<b>Total:</b> = %s" % _format_range_minutes(
        total_min, total_max
    )
    break_line = "<br>\r\n"
    if not lines:
        body = total_line
    else:
        body = "%s%s%s" % (total_line, break_line, break_line.join(lines))
    return "<div style='min-width:320px;white-space:nowrap'>%s</div>" % body


def _remaining_rows(targets: list[tuple[int, int]]) -> list[tuple[int, int, float]]:
    rows = []
    for did, target in targets:
        if _suppress_new_complete(did):
            continue
        rows.append((did, target, _progress_minutes_today(did, target)))
    return rows


def _current_display_progress(w: QWidget) -> tuple[float, float, str | None]:
    spent = w.current_base_spent + _live_elapsed_minutes(w)
    target = int(w.current_target)
    if getattr(w, "current_suppress_complete", False):
        return target, target, PROGRESS_COMPLETE
    if target > 0 and spent >= target:
        grace = _over_limit_grace_minutes(target)
        return max(0.0, spent - target), grace, PROGRESS_OVERTIME
    return spent, target, None


def _live_elapsed_minutes(w: QWidget) -> float:
    if getattr(w, "live_started_at", None) is None:
        return 0.0
    elapsed = max(0.0, time.monotonic() - w.live_started_at)
    max_seconds = max(0, int(getattr(w, "live_max_seconds", 0) or 0))
    if max_seconds:
        elapsed = min(elapsed, max_seconds)
    return elapsed / 60.0


def _tick_progress_widget(w: QWidget) -> None:
    if not w.isVisible() or getattr(mw, "state", None) != "review":
        w.live_timer.stop()
        return
    live_minutes = _live_elapsed_minutes(w)
    if getattr(w, "current_tdid", None) is not None:
        current_spent, current_target, current_color = _current_display_progress(w)
        current_rows = []
        if not getattr(w, "current_suppress_complete", False):
            current_rows = [
                (
                    w.current_tdid,
                    int(w.current_target),
                    float(w.current_base_spent),
                )
            ]
        _set_progress_row(
            w.current_name,
            "Current deck",
            w.current_bar,
            w.current_count,
            current_spent,
            current_target,
            color_override=current_color,
            tooltip_spent=w.current_base_spent + live_minutes,
            tooltip_target=w.current_target,
            count_tooltip=_remaining_breakout_tooltip(
                current_rows,
                live_did=w.current_tdid,
                live_minutes=live_minutes,
            ),
        )
    _set_progress_row(
        w.overall_name,
        "Overall",
        w.overall_bar,
        w.overall_count,
        w.overall_base_spent + min(live_minutes, w.overall_live_room),
        w.overall_target,
        count_tooltip=_remaining_breakout_tooltip(
            w.overall_remaining_rows,
            live_did=getattr(w, "current_tdid", None),
            live_minutes=live_minutes,
        ),
    )


def _set_current_row_visible(w: QWidget, visible: bool) -> None:
    for attr in ("current_name", "current_bar", "current_count"):
        getattr(w, attr).setVisible(visible)


def _refresh_progress_widget(current_tdid: int | None = None) -> None:
    try:
        w = _progress_widget()
        state = getattr(mw, "state", None)
        if state not in ("review", "deckBrowser"):
            w.setVisible(False)
            w.live_timer.stop()
            return

        targets = _configured_target_decks()
        if not targets:
            w.setVisible(False)
            w.live_timer.stop()
            return

        review_card = _current_review_card()
        if state == "deckBrowser":
            current_tdid = None
        else:
            if review_card is not None:
                current_tdid = _target_deck_for(review_card.did)
                w.live_max_seconds = _max_answer_seconds_for_did(review_card.did)
            else:
                current_tdid = current_tdid or _current_target_deck()
                w.live_max_seconds = DEFAULT_MAX_ANSWER_SECONDS
        w.current_tdid = current_tdid
        w.live_started_at = time.monotonic() if current_tdid is not None else None
        if current_tdid is not None:
            current_target = _target_for(current_tdid)
            if current_target > 0:
                w.current_suppress_complete = _suppress_new_complete(current_tdid)
                if w.current_suppress_complete:
                    w.live_started_at = None
                current_spent = _progress_minutes_today(
                    current_tdid, current_target
                )
                w.current_base_spent = current_spent
                w.current_target = current_target
                display_spent, display_target, display_color = (
                    _current_display_progress(w)
                )
                _set_progress_row(
                    w.current_name,
                    "Current deck",
                    w.current_bar,
                    w.current_count,
                    display_spent,
                    display_target,
                    color_override=display_color,
                    tooltip_spent=current_spent,
                    tooltip_target=current_target,
                    count_tooltip=_remaining_breakout_tooltip(
                        []
                        if w.current_suppress_complete
                        else [(current_tdid, current_target, current_spent)]
                    ),
                )
                _set_current_row_visible(w, True)
            else:
                w.current_tdid = None
                w.current_suppress_complete = False
                _set_current_row_visible(w, False)
        else:
            w.current_tdid = None
            w.current_suppress_complete = False
            _set_current_row_visible(w, False)

        overall_target = sum(target for _, target in targets)
        overall_spent = sum(
            min(_progress_minutes_today(did, target), target)
            for did, target in targets
        )
        overall_remaining_rows = _remaining_rows(targets)
        current_room = 0.0
        if (
            current_tdid is not None
            and current_target > 0
            and not _suppress_new_complete(current_tdid)
        ):
            current_room = max(0.0, current_target - current_spent)
        w.overall_base_spent = overall_spent
        w.overall_target = overall_target
        w.overall_live_room = current_room
        w.overall_remaining_rows = overall_remaining_rows
        _set_progress_row(w.overall_name, "Overall",
                          w.overall_bar, w.overall_count,
                          overall_spent, overall_target,
                          count_tooltip=_remaining_breakout_tooltip(
                              overall_remaining_rows
                          ))
        w.setVisible(True)
        if w.live_started_at is not None:
            w.live_timer.start()
        else:
            w.live_timer.stop()
    except Exception as exc:  # pragma: no cover - never break Anki's UI
        print("TimePerDeck: progress refresh failed:", exc)


# Configuration ---------------------------------------------------------------

DEFAULT_CONFIG = {
    "postpone_fails_after_target": True,
    "postpone_all_after_grace": True,
    "suppress_new_cards": False,
    "backlog_suppress_new_cards": False,
    "backlog_bury_after_fail": True,
    "backlog_bury_after_fail_limit": DEFAULT_BACKLOG_FAIL_LIMIT,
    "over_limit_grace_percent": DEFAULT_OVER_LIMIT_GRACE_PERCENT,
    "recommended_order": True,
    "targets": {},
}


def _legacy_backlog_config() -> dict:
    try:
        return mw.addonManager.getConfig("EmergencyBacklogClear") or {}
    except Exception:
        return {}


def _meta_config() -> dict:
    try:
        path = os.path.join(ADDON_DIR, "meta.json")
        with open(path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        config = meta.get("config", {})
        return config if isinstance(config, dict) else {}
    except Exception:
        return {}


def _config() -> dict:
    raw = mw.addonManager.getConfig(__name__) or {}
    meta = _meta_config()
    if meta:
        raw = {**raw, **meta}
    cfg = {**DEFAULT_CONFIG, **raw}
    legacy = _legacy_backlog_config()

    if "backlog_suppress_new_cards" not in raw:
        if "suppress_new_cards" in legacy or "enabled" in legacy:
            cfg["backlog_suppress_new_cards"] = bool(
                legacy.get("suppress_new_cards", legacy.get("enabled", False))
            )
        else:
            cfg["backlog_suppress_new_cards"] = bool(
                raw.get("suppress_new_cards", DEFAULT_CONFIG["suppress_new_cards"])
            )

    if "backlog_bury_after_fail" not in raw:
        cfg["backlog_bury_after_fail"] = bool(
            legacy.get(
                "bury_after_fail",
                legacy.get(
                    "enabled", DEFAULT_CONFIG["backlog_bury_after_fail"]
                ),
            )
        )

    if "backlog_bury_after_fail_limit" not in raw:
        try:
            cfg["backlog_bury_after_fail_limit"] = max(
                1,
                int(legacy.get("bury_after_fail_limit", DEFAULT_BACKLOG_FAIL_LIMIT)),
            )
        except (TypeError, ValueError):
            cfg["backlog_bury_after_fail_limit"] = DEFAULT_BACKLOG_FAIL_LIMIT

    return cfg


def _save_config(cfg: dict) -> None:
    mw.addonManager.writeConfig(__name__, cfg)


def _targets() -> dict:
    return _config().get("targets", {}) or {}


def _target_for(did: int) -> int:
    try:
        return int(_targets().get(str(did), 0))
    except (TypeError, ValueError):
        return 0


def _postpone_fails_enabled() -> bool:
    return bool(_config()["postpone_fails_after_target"])


def _postpone_over_limit_enabled() -> bool:
    return bool(_config()["postpone_all_after_grace"])


def _recommended_order_enabled() -> bool:
    return bool(_config()["recommended_order"])


def _suppress_new_cards_enabled() -> bool:
    # The visible suppress control is now Backlog Clear's runtime-only filter.
    # Keep the old timer-specific completion/bury path off.
    return False


def _backlog_suppress_new_cards_enabled() -> bool:
    return bool(_config()["backlog_suppress_new_cards"])


def _backlog_bury_after_fail_enabled() -> bool:
    return bool(_config()["backlog_bury_after_fail"])


def _backlog_bury_after_fail_limit() -> int:
    try:
        return max(1, int(_config()["backlog_bury_after_fail_limit"]))
    except (TypeError, ValueError):
        return DEFAULT_BACKLOG_FAIL_LIMIT


def _fail_limit_label(limit=None) -> str:
    limit = _backlog_bury_after_fail_limit() if limit is None else max(1, int(limit))
    return "%d %s" % (limit, "fail" if limit == 1 else "fails")


def _over_limit_grace_percent() -> int:
    try:
        return max(0, int(_config().get(
            "over_limit_grace_percent",
            DEFAULT_OVER_LIMIT_GRACE_PERCENT,
        )))
    except (TypeError, ValueError):
        return DEFAULT_OVER_LIMIT_GRACE_PERCENT


def _over_limit_grace_minutes(target: int) -> float:
    return max(0, target) * (_over_limit_grace_percent() / 100.0)


# Backlog clear ---------------------------------------------------------------

def _load_data() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_data(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as exc:  # pragma: no cover - best effort persistence
        print("TimePerDeck: could not save backlog fail counts:", exc)


def _record_fail_and_should_bury(card_id: int) -> bool:
    today = mw.col.sched.today
    data = _load_data()
    if data.get("day") != today:
        data = {"day": today, "counts": {}}
    counts = data["counts"]
    key = str(card_id)
    counts[key] = counts.get(key, 0) + 1
    n = counts[key]
    _save_data(data)
    return n >= _backlog_bury_after_fail_limit()


def _save_deck(deck: dict) -> None:
    decks = mw.col.decks
    for method_name in ("update", "save"):
        method = getattr(decks, method_name, None)
        if method is None:
            continue
        try:
            method(deck)
            return
        except Exception:
            pass
    raise RuntimeError("Could not save deck settings.")


def _set_deck_today_only_new_limit_to_zero(deck: dict, today: int) -> int:
    if deck.get("dyn", 0):
        return 0
    current = deck.get("newLimitToday") or {}
    if current.get("limit") == 0 and current.get("today") == today:
        return 0
    deck["newLimitToday"] = {"limit": 0, "today": today}
    _save_deck(deck)
    return 1


def _set_target_today_only_new_limit_to_zero(did: int) -> int:
    deck = mw.col.decks.get(did)
    if not deck:
        return 0
    return _set_deck_today_only_new_limit_to_zero(
        deck,
        int(mw.col.sched.today),
    )


def _set_all_today_only_new_limits_to_zero() -> int:
    today = int(mw.col.sched.today)
    changed = 0

    for deck in mw.col.decks.all():
        changed += _set_deck_today_only_new_limit_to_zero(deck, today)

    return changed


def _confirm_today_only_new_zero(parent: QDialog) -> None:
    if not askUser(
        "Set the Today Only new-card limit to 0 on all normal decks?",
        parent=parent,
    ):
        return

    changed = _set_all_today_only_new_limits_to_zero()
    tooltip("Backlog Clear: set Today Only new limit to 0 on %d decks." % changed)
    if mw.deckBrowser:
        mw.deckBrowser.refresh()


# Time accounting -------------------------------------------------------------

def _day_start_ms() -> int:
    """Epoch milliseconds of the start of the current Anki day."""
    return (mw.col.sched.day_cutoff - 86400) * 1000


def _child_ids_str(did: int) -> str:
    return ids2str(mw.col.decks.deck_and_child_ids(did))


def _minutes_today(did: int) -> float:
    """Minutes spent answering cards in this deck (and sub-decks) today."""
    dids = _child_ids_str(did)
    ms = mw.col.db.scalar(
        "select ifnull(sum(time), 0) from revlog where id >= ? "
        f"and cid in (select id from cards where did in {dids})",
        _day_start_ms(),
    )
    return (ms or 0) / 60000.0


def _new_cards_remaining(did: int) -> int:
    """Count of not-yet-introduced new cards in this deck (and sub-decks)."""
    dids = _child_ids_str(did)
    return mw.col.db.scalar(
        f"select count() from cards where queue = {_QUEUE_NEW} and did in {dids}"
    ) or 0


def _new_cards_remaining_exact(did: int) -> int:
    """Count of not-yet-introduced new cards directly in this deck."""
    return mw.col.db.scalar(
        "select count() from cards where queue = ? and did = ?",
        _QUEUE_NEW,
        did,
    ) or 0


def _due_review_cards_remaining(did: int) -> int:
    """Count review/relearning cards that are due now in this deck tree."""
    dids = _child_ids_str(did)
    today = mw.col.sched.today
    now = int(time.time())
    return mw.col.db.scalar(
        f"select count() from cards where did in {dids} and ("
        f"(queue = {_QUEUE_REV} and due <= ?) or "
        f"(queue = {_QUEUE_LEARN} and due <= ?) or "
        f"(queue = {_QUEUE_DAY_LEARN} and due <= ?))",
        today,
        now,
        today,
    ) or 0


def _available_cards_remaining(did: int) -> int:
    """Count non-new cards that can keep a suppressed-new deck unfinished."""
    return _due_review_cards_remaining(did)


def _suppress_new_complete(did: int) -> bool:
    # Suppressing new cards is only a runtime queue filter. It should not make a
    # deck count as complete, change progress, or trigger burying.
    return False


def _progress_minutes_today(did: int, target: int) -> float:
    spent = _minutes_today(did)
    if target > 0 and _suppress_new_complete(did):
        return max(spent, float(target))
    return spent


def _state_for(did: int) -> str | None:
    """Return UNDER / DONE / OVER for a deck, or None if it has no target."""
    target = _target_for(did)
    if target <= 0:
        return None
    spent = _minutes_today(did)
    if spent < target:
        return UNDER
    if (
        not _postpone_over_limit_enabled()
        or spent <= target + _over_limit_grace_minutes(target)
    ):
        return DONE
    return OVER


def _target_deck_for(deck_id: int) -> int | None:
    """Find the configured target deck that owns `deck_id` (itself or ancestor).

    If several targets match (nested), return the most specific one."""
    matches = []
    for sdid, minutes in _targets().items():
        try:
            tdid = int(sdid)
        except (TypeError, ValueError):
            continue
        if int(minutes) <= 0:
            continue
        children = mw.col.decks.deck_and_child_ids(tdid)
        if deck_id in children:
            matches.append((len(children), tdid))
    if not matches:
        return None
    matches.sort()  # smallest child set first == most specific
    return matches[0][1]


# State application -----------------------------------------------------------

def _recompute_blocked() -> None:
    """Rebuild the set of deck ids whose new cards must be suppressed."""
    global _blocked
    blocked: set[int] = set()
    for sdid, minutes in _targets().items():
        try:
            did = int(sdid)
        except (TypeError, ValueError):
            continue
        if int(minutes) <= 0:
            continue
        if _state_for(did) in (DONE, OVER) or _suppress_new_complete(did):
            blocked.update(int(x) for x in mw.col.decks.deck_and_child_ids(did))
    _blocked = blocked


def _bury_over(did: int) -> int:
    """Bury remaining new, review, and learning cards until tomorrow.

    Only cards the user has actually answered at least once (reps > 0) are
    buried. Fresh, never-studied cards are left alone so that a large premade
    backlog sitting in a targeted deck's sub-tree can't get bulk-buried when the
    time target is passed.
    """
    dids = _child_ids_str(did)
    today = mw.col.sched.today
    ids = mw.col.db.list(
        f"select id from cards where did in {dids} "
        f"and (queue in ({_QUEUE_NEW}, {_QUEUE_LEARN}, {_QUEUE_DAY_LEARN}) "
        f"or (queue = {_QUEUE_REV} and due <= ?)) "
        f"and reps > 0",
        today,
    )
    if ids:
        mw.col.sched.bury_cards(ids, manual=True)
    return len(ids)


def _bury_remaining_new(did: int) -> int:
    """Bury remaining new cards in a done deck until tomorrow.

    Skips never-studied cards (reps = 0) so untouched new-card backlogs are not
    swept up; only cards with prior review history are buried.
    """
    dids = _child_ids_str(did)
    ids = mw.col.db.list(
        f"select id from cards where did in {dids} and queue = {_QUEUE_NEW} "
        f"and reps > 0"
    )
    if ids:
        mw.col.sched.bury_cards(ids, manual=True)
    return len(ids)


def _bury_again_after_target(did: int, card, ease) -> int:
    """Bury the answered card on Again once a deck has reached its target."""
    # ease == 1 is the "Again" button.
    if (
        ease != 1
        or not _postpone_fails_enabled()
        or _state_for(did) != DONE
        or (card.reps or 0) <= 0
    ):
        return 0
    mw.col.sched.bury_cards([card.id], manual=True)
    tooltip("Time Per Deck: buried Again card until tomorrow.", period=1500)
    return 1


def _bury_backlog_again(card, ease) -> int:
    # ease == 1 is the "Again" button.
    if ease != 1 or not _backlog_bury_after_fail_enabled():
        return 0
    if (card.reps or 0) <= 0:
        return 0
    if not _record_fail_and_should_bury(card.id):
        return 0
    mw.col.sched.bury_cards([card.id], manual=True)
    tooltip("Backlog Clear: buried after %s." % _fail_limit_label(), period=1500)
    return 1


def _due_tree_count(node, attr: str) -> int:
    try:
        return int(getattr(node, attr, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _has_available_work(node) -> bool:
    return any(
        _due_tree_count(node, attr) > 0
        for attr in ("new_count", "review_count", "learn_count")
    )


def _maybe_extend(did: int) -> int:
    """Top up new cards only after an under-target deck has no work left."""
    try:
        node = mw.col.sched.deck_due_tree(did)
        if node is None or _has_available_work(node):
            return 0
        if _new_cards_remaining(did) <= 0:
            return 0
        extended = 0
        child_ids = [int(x) for x in mw.col.decks.deck_and_child_ids(did)]
        deck_ids = [did] + [x for x in child_ids if x != did]
        for deck_id in deck_ids:
            if deck_id != did and _new_cards_remaining_exact(deck_id) <= 0:
                continue
            mw.col._backend.extend_limits(
                deck_id=deck_id, new_delta=EXTEND_BATCH, review_delta=0
            )
            extended += EXTEND_BATCH
        return extended
    except Exception as exc:  # pragma: no cover - never break studying
        print("TimePerDeck: extend failed:", exc)
        return 0


def _apply_over(did: int) -> int:
    """Run the over-limit bury for a deck and queue a delayed re-fire."""
    changed = _bury_over(did) + _set_target_today_only_new_limit_to_zero(did)
    _schedule_over_refire(did)
    return changed


def _schedule_over_refire(did: int) -> None:
    """(Re)start the 10-second timer that re-fires the over-limit bury."""
    global _over_refire_timer
    try:
        _over_refire_dids.add(int(did))
        if _over_refire_timer is None:
            _over_refire_timer = QTimer(mw)
            _over_refire_timer.setSingleShot(True)
            _over_refire_timer.timeout.connect(_refire_over_bury)
        # Restart the window so a burst of burys collapses into one re-fire.
        _over_refire_timer.start(OVER_REFIRE_MS)
    except Exception as exc:  # pragma: no cover - never break studying
        print("TimePerDeck: could not schedule over-limit re-fire:", exc)


def _refire_over_bury() -> None:
    """Re-run the over-limit bury on decks that are still over their target."""
    dids = list(_over_refire_dids)
    _over_refire_dids.clear()
    try:
        changed = 0
        for did in dids:
            if _state_for(did) != OVER:
                continue
            changed += _bury_over(did) + _set_target_today_only_new_limit_to_zero(did)
        if changed:
            _recompute_blocked()
            _refresh_progress_widget()
            if getattr(mw, "state", None) == "deckBrowser" and mw.deckBrowser:
                mw.deckBrowser.refresh()
    except Exception as exc:  # pragma: no cover - never break studying
        print("TimePerDeck: over-limit re-fire failed:", exc)


def _apply_for_deck(did: int, *, allow_extend: bool = True) -> int:
    """Apply target-deck actions. Returns changed count."""
    state = _state_for(did)
    if state == OVER:
        return _apply_over(did)
    if state == DONE:
        return _bury_remaining_new(did)
    if state == UNDER and _suppress_new_complete(did):
        return _bury_remaining_new(did)
    if state == UNDER and allow_extend:
        return _maybe_extend(did)
    return 0


# Reviewer: suppress new cards live for done / over decks ---------------------

def _queued_card_id(queued_card) -> int | None:
    card = getattr(queued_card, "card", None)
    for attr in ("id", "card_id"):
        try:
            value = getattr(card, attr)
        except Exception:
            continue
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _failed_today_card_ids(card_ids: list[int]) -> set[int]:
    if not card_ids:
        return set()
    return {
        int(cid)
        for cid in mw.col.db.list(
            "select distinct cid from revlog "
            f"where id >= ? and ease = 1 and cid in {ids2str(card_ids)}",
            _day_start_ms(),
        )
    }


def _recommended_order_cards(cards: list) -> list:
    review = []
    rereview = []
    new = []
    review_card_ids = []
    review_candidates = []
    for card in cards:
        if card.queue == QueuedCards.NEW:
            new.append(card)
        else:
            cid = _queued_card_id(card)
            review_candidates.append((card, cid))
            if cid is not None:
                review_card_ids.append(cid)

    failed_today = _failed_today_card_ids(review_card_ids)
    for card, cid in review_candidates:
        if cid is not None and cid in failed_today:
            rereview.append(card)
        else:
            review.append(card)
    return review + rereview + new


def _patched_get_queued_cards(self, *, fetch_limit: int = 1,
                              intraday_learning_only: bool = False):
    global _blocked
    if _blocked is None:
        try:
            _recompute_blocked()
        except Exception:
            _blocked = set()

    suppress_all_new = _backlog_suppress_new_cards_enabled()
    recommended_order = _recommended_order_enabled()
    if not _blocked and not suppress_all_new and not recommended_order:
        return _orig_get_queued_cards(
            self, fetch_limit=fetch_limit,
            intraday_learning_only=intraday_learning_only,
        )

    result = _orig_get_queued_cards(
        self, fetch_limit=max(fetch_limit, _INTERNAL_FETCH),
        intraday_learning_only=intraday_learning_only,
    )

    def should_drop(c) -> bool:
        return (
            c.queue == QueuedCards.NEW
            and (suppress_all_new or c.card.deck_id in _blocked)
        )

    kept = [c for c in result.cards if not should_drop(c)]
    removed = sum(1 for c in result.cards if should_drop(c))
    if recommended_order:
        kept = _recommended_order_cards(kept)

    filtered = QueuedCards()
    filtered.CopyFrom(result)
    del filtered.cards[:]
    filtered.cards.extend(kept[:fetch_limit])
    filtered.new_count = 0 if suppress_all_new else max(0, result.new_count - removed)
    return filtered


def _install_scheduler_patch() -> None:
    global _orig_get_queued_cards
    if _orig_get_queued_cards is None:
        _orig_get_queued_cards = V3Scheduler.get_queued_cards
        V3Scheduler.get_queued_cards = _patched_get_queued_cards


# Hooks: study events ---------------------------------------------------------

def _on_reviewer_did_answer_card(reviewer, card, ease) -> None:
    try:
        backlog_buried = _bury_backlog_again(card, ease)
        tdid = _target_deck_for(card.did)
        _recompute_blocked()
        if tdid is not None:
            if not backlog_buried:
                _bury_again_after_target(tdid, card, ease)
            _apply_for_deck(tdid)
        _refresh_progress_widget(tdid)
    except Exception as exc:  # pragma: no cover - never break the reviewer
        print("TimePerDeck: answer hook failed:", exc)


def _on_state_did_change(new_state, old_state) -> None:
    global _blocked
    try:
        if new_state == "review":
            _recompute_blocked()
            tdid = _target_deck_for(mw.col.decks.get_current_id())
            if tdid is not None:
                _apply_for_deck(tdid)
            _refresh_progress_widget(tdid)
        elif new_state == "overview":
            # The user browsed into a deck from the deck list. Re-run the check
            # so an over-limit deck buries its eligible cards again (and queues
            # the delayed re-fire) before they start studying.
            _recompute_blocked()
            tdid = _target_deck_for(mw.col.decks.get_current_id())
            if tdid is not None:
                _apply_for_deck(tdid, allow_extend=False)
            _refresh_progress_widget()
        elif new_state == "deckBrowser":
            _blocked = None  # force a fresh recompute next time it's needed
            _refresh_progress_widget()
        else:
            _refresh_progress_widget()
    except Exception as exc:  # pragma: no cover
        print("TimePerDeck: state hook failed:", exc)


# Hooks: deck browser ---------------------------------------------------------

COUNT_CELL_RE = re.compile(
    r"<td\b(?=[^>]*(?:\balign=(?:[\"']?end[\"']?|end\b)|"
    r"\bclass=(?:[\"'][^\"']*\bcount\b[^\"']*[\"']|[^ >]*\bcount\b)))"
    r"[^>]*>.*?</td>",
    re.S,
)
DECK_OPTS_RE = re.compile(
    r"<td\b(?=[^>]*\bclass=(?:[\"']?opts[\"']?|opts\b))"
    r"[^>]*>.*?pycmd\([\"']opts:(\d+)[\"']\);.*?</td>",
    re.S,
)
DECK_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.S)


def _progress_cell_html(did: int) -> str:
    target = _target_for(did)
    if target <= 0:
        return ""
    spent = _progress_minutes_today(did, target)
    remaining = max(0, int(round(target - spent)))
    pct = max(0, min(100, int(round((spent / target) * 100))))
    color = PROGRESS_COMPLETE if spent >= target else PROGRESS_ACTIVE
    label = "&#x2714;&#xfe0f;" if spent >= target else "%d min" % remaining
    label_color = PURPLE if spent >= target else PROGRESS_TEXT
    title = "%d of %d mins studied today" % (
        int(spent),
        target,
    )
    return (
        "<td align=center class='count tpd-count-progress' colspan=3 "
        "style='min-width:170px;padding-left:8px;padding-right:8px' title='%s'>"
        "<span style='display:flex;align-items:center;gap:7px;width:100%%'>"
        "<span style='color:%s;display:inline-block;min-width:7ch;"
        "text-align:right;white-space:nowrap;"
        "font-variant-numeric:tabular-nums'>%s</span>"
        "<span style='display:block;flex:1;height:9px;"
        "background:%s;border-radius:5px;overflow:hidden'>"
        "<span style='display:block;width:%d%%;height:100%%;background:%s'></span>"
        "</span></span></td>"
        % (title, label_color, label, PROGRESS_TRACK, pct, color)
    )


def _deck_ids_from_render_tree(deck_browser) -> list[int]:
    render_data = getattr(deck_browser, "_render_data", None)
    root = getattr(render_data, "tree", None)
    ids: list[int] = []

    def walk(node) -> None:
        did = int(getattr(node, "deck_id", 0) or 0)
        if did:
            ids.append(did)
        for child in getattr(node, "children", []) or []:
            walk(child)

    for child in getattr(root, "children", []) or []:
        walk(child)
    return ids


def _replace_count_cells(row: str, progress_cell: str) -> str:
    cells = list(COUNT_CELL_RE.finditer(row))
    if len(cells) < 3:
        return row

    first = cells[-3]
    last = cells[-1]
    return row[: first.start()] + progress_cell + row[last.end() :]


def _inject_deck_progress(tree: str, deck_browser=None) -> str:
    render_ids = iter(_deck_ids_from_render_tree(deck_browser) if deck_browser else [])

    def repl(m: "re.Match") -> str:
        row = m.group(0)
        if len(list(COUNT_CELL_RE.finditer(row))) < 3:
            return row

        opts_match = DECK_OPTS_RE.search(row)
        if opts_match:
            did = int(opts_match.group(1))
        else:
            try:
                did = next(render_ids)
            except StopIteration:
                return row

        progress_cell = _progress_cell_html(did)
        if not progress_cell:
            return row

        return _replace_count_cells(row, progress_cell)

    return DECK_ROW_RE.sub(repl, tree)


def _deck_progress_info(did: int, target: int) -> dict[str, str | int | list[str]]:
    spent = _progress_minutes_today(did, target)
    remaining = max(0, int(round(target - spent)))
    pct = max(0, min(100, int(round((spent / target) * 100))))
    color = PROGRESS_COMPLETE if spent >= target else PROGRESS_ACTIVE
    full_name = _deck_name(did)
    short_name = full_name.split("::")[-1]
    names = [full_name]
    if short_name != full_name:
        names.append(short_name)
    return {
        "pct": pct,
        "color": color,
        "track": PROGRESS_TRACK,
        "title": "%d of %d mins studied today" % (int(spent), target),
        "label": (
            "\u2714\ufe0f"
            if spent >= target
            else "%d min" % remaining
        ),
        "labelColor": PURPLE if spent >= target else PROGRESS_TEXT,
        "names": names,
    }


def _deck_progress_payload(deck_browser=None) -> dict[str, object]:
    by_id: dict[str, dict[str, str | int | list[str]]] = {}
    for sdid, minutes in _targets().items():
        try:
            did = int(sdid)
            target = int(minutes)
        except (TypeError, ValueError):
            continue
        if target <= 0:
            continue

        by_id[str(did)] = _deck_progress_info(did, target)
    return {
        "byId": by_id,
        "rowDeckIds": [str(did) for did in _deck_ids_from_render_tree(deck_browser)],
    }


def _deck_progress_script(deck_browser=None) -> str:
    payload = _deck_progress_payload(deck_browser)
    if not payload["byId"]:
        return ""

    data = json.dumps(payload)
    return """
<script id="tpd-deck-progress-script">
(function() {
  const progressData = %s;
  const progressByDeck = progressData.byId || {};
  const rowDeckIds = progressData.rowDeckIds || [];

  function makeCell(info) {
    const cell = document.createElement("td");
    cell.className = "count tpd-count-progress";
    cell.colSpan = 3;
    cell.align = "center";
    cell.title = info.title;
    cell.style.minWidth = "170px";
    cell.style.paddingLeft = "8px";
    cell.style.paddingRight = "8px";

    const wrap = document.createElement("span");
    wrap.style.display = "flex";
    wrap.style.alignItems = "center";
    wrap.style.gap = "7px";
    wrap.style.width = "100%%";

    const label = document.createElement("span");
    label.textContent = info.label;
    label.style.color = info.labelColor;
    label.style.display = "inline-block";
    label.style.minWidth = "7ch";
    label.style.textAlign = "right";
    label.style.whiteSpace = "nowrap";
    label.style.fontVariantNumeric = "tabular-nums";

    const track = document.createElement("span");
    track.style.display = "block";
    track.style.flex = "1";
    track.style.height = "9px";
    track.style.background = info.track;
    track.style.borderRadius = "5px";
    track.style.overflow = "hidden";

    const fill = document.createElement("span");
    fill.style.display = "block";
    fill.style.width = info.pct + "%%";
    fill.style.height = "100%%";
    fill.style.background = info.color;

    track.appendChild(fill);
    wrap.appendChild(label);
    wrap.appendChild(track);
    cell.appendChild(wrap);
    return cell;
  }

  function deckCellText(row) {
    const deckCell = Array.from(row.children).find(function(cell) {
      return cell.tagName === "TD"
        && !cell.classList.contains("count")
        && !cell.classList.contains("opts");
    });
    return deckCell ? deckCell.textContent.replace(/\\s+/g, " ").trim() : "";
  }

  function infoForRow(row, index) {
    const optMatch = row.innerHTML.match(/opts:(\\d+)/);
    if (optMatch && progressByDeck[optMatch[1]]) {
      return progressByDeck[optMatch[1]];
    }

    const rowDid = rowDeckIds[index];
    if (rowDid && progressByDeck[rowDid]) {
      return progressByDeck[rowDid];
    }

    const text = deckCellText(row);
    return Object.values(progressByDeck).find(function(info) {
      return (info.names || []).some(function(name) {
        return name && text.indexOf(name) !== -1;
      });
    });
  }

  function paint() {
    Array.from(document.querySelectorAll("tr"))
      .filter(function(row) {
        return row.querySelectorAll('td[align="end"], td.count').length >= 3;
      })
      .forEach(function(row, index) {
      if (row.querySelector(".tpd-count-progress")) {
        return;
      }

      const info = infoForRow(row, index);
      if (!info) {
        return;
      }

      const cells = Array.from(row.querySelectorAll('td[align="end"], td.count'));
      if (cells.length < 3) {
        return;
      }

      const first = cells[cells.length - 3];
      const second = cells[cells.length - 2];
      const third = cells[cells.length - 1];
      first.replaceWith(makeCell(info));
      second.remove();
      third.remove();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", paint);
  } else {
    paint();
  }
  setTimeout(paint, 100);
  setTimeout(paint, 500);
})();
</script>
""" % data


def _on_deck_browser_will_render_content(deck_browser, content) -> None:
    try:
        content.tree = _inject_deck_progress(content.tree, deck_browser)
        content.stats += _deck_progress_script(deck_browser)
    except Exception as exc:  # pragma: no cover - never break the deck list
        print("TimePerDeck: deck progress injection failed:", exc)


def _on_deck_browser_did_render(deck_browser) -> None:
    global _applying
    if _applying:
        return
    _applying = True
    try:
        changed = 0
        for sdid, minutes in _targets().items():
            try:
                did = int(sdid)
            except (TypeError, ValueError):
                continue
            if int(minutes) <= 0:
                continue
            changed += _apply_for_deck(did, allow_extend=False)
        _recompute_blocked()
        if changed:
            deck_browser.refresh()
    except Exception as exc:  # pragma: no cover
        print("TimePerDeck: deck browser apply failed:", exc)
    finally:
        _applying = False


# Hooks: bottom-bar button ----------------------------------------------------

def _on_webview_will_set_content(web_content, context) -> None:
    if isinstance(context, DeckBrowser):
        if _backlog_suppress_new_cards_enabled():
            web_content.head += (
                "<style id='tpd-hide-new'>"
                "table tr > th:nth-child(2),"
                "table tr > td:nth-child(2):not(.tpd-count-progress)"
                "{display:none !important;}"
                "</style>"
            )
        return

    if not isinstance(context, DeckBrowserBottomBar):
        return
    web_content.body += """
<script>
(function() {
  function place() {
    var anchor = document.getElementById('ebc-btn');
    if (!anchor) anchor = document.querySelector('button[onclick*="import"]');
    if (!anchor) { setTimeout(place, 60); return; }
    var existing = document.getElementById('tpd-btn');
    if (existing) existing.remove();
    var btn = document.createElement('button');
    btn.id = 'tpd-btn';
    btn.textContent = 'Minutes per deck';
    btn.title = 'Set per-deck daily time targets.';
    btn.setAttribute('style', 'margin-left:6px;');
    btn.addEventListener('click', function() { pycmd('%s'); });
    anchor.parentNode.insertBefore(btn, anchor.nextSibling);
  }
  place();
})();
</script>
""" % PYCMD_SETTINGS


def _on_js_message(handled, message: str, context):
    if message == PYCMD_SETTINGS:
        _open_settings()
        return (True, None)
    return handled


# Settings dialog -------------------------------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("Minutes per deck")
        self.resize(840, 720)
        cfg = _config()
        targets = cfg.get("targets", {}) or {}

        layout = QVBoxLayout(self)

        options_row = QWidget()
        options_layout = QHBoxLayout(options_row)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.setSpacing(28)

        addon_options = QWidget()
        addon_layout = QVBoxLayout(addon_options)
        addon_layout.setContentsMargins(0, 0, 0, 0)
        addon_layout.addWidget(QLabel("<b>Addon Options:</b>"))
        self.postpone_fails = QCheckBox(
            "When daily timer limit is reached, begin postponing (bury) "
            "failed review cards to the next day"
        )
        self.postpone_fails.setChecked(
            bool(cfg["postpone_fails_after_target"])
        )
        addon_layout.addWidget(self.postpone_fails)

        over_row = QWidget()
        over_layout = QHBoxLayout(over_row)
        over_layout.setContentsMargins(0, 0, 0, 0)
        self.postpone_all = QCheckBox()
        self.postpone_all.setToolTip(
            "Automatically postpone all remaining cards after the grace period."
        )
        self.postpone_all.setChecked(bool(cfg["postpone_all_after_grace"]))
        self.over_limit_grace_percent = NoWheelSpinBox()
        self.over_limit_grace_percent.setRange(0, 1000)
        self.over_limit_grace_percent.setSuffix("%")
        self.over_limit_grace_percent.setStyleSheet(SPINBOX_NO_ARROWS)
        self.over_limit_grace_percent.setValue(_over_limit_grace_percent())
        over_layout.addWidget(self.postpone_all)
        over_layout.addWidget(self.over_limit_grace_percent)
        over_layout.addWidget(
            QLabel(
                "past the deck's time limit, postpone (bury) "
                "remaining deck's cards to the next day"
            )
        )
        over_layout.addStretch(1)
        self.over_limit_grace_percent.setEnabled(self.postpone_all.isChecked())
        self.postpone_all.toggled.connect(
            self.over_limit_grace_percent.setEnabled
        )
        addon_layout.addWidget(over_row)
        self.recommended_order = QCheckBox(
            "Recommended Card Order: Due first reviews -> "
            "Re-reviews (fails) -> New cards"
        )
        self.recommended_order.setToolTip(
            "Present due review cards that have not failed today before "
            "review cards that have failed today, and keep new cards last."
        )
        self.recommended_order.setChecked(bool(cfg["recommended_order"]))
        addon_layout.addWidget(self.recommended_order)
        addon_layout.addStretch(1)

        backlog_options = QWidget()
        backlog_layout = QVBoxLayout(backlog_options)
        backlog_layout.setContentsMargins(0, 0, 0, 0)
        backlog_layout.addWidget(QLabel("<b>Backlog Clear:</b>"))

        bury_after_fail_row = QWidget()
        bury_after_fail_layout = QHBoxLayout(bury_after_fail_row)
        bury_after_fail_layout.setContentsMargins(0, 0, 0, 0)
        self.backlog_bury_after_fail = QCheckBox("Auto-bury after")
        self.backlog_bury_after_fail.setChecked(
            bool(cfg["backlog_bury_after_fail"])
        )
        self.backlog_bury_after_fail.setToolTip(
            "After you answer a card Again this many times today, bury it "
            "until tomorrow."
        )
        self.backlog_bury_after_fail_limit = NoWheelSpinBox()
        self.backlog_bury_after_fail_limit.setRange(1, 99)
        self.backlog_bury_after_fail_limit.setStyleSheet(SPINBOX_NO_ARROWS)
        self.backlog_bury_after_fail_limit.setValue(
            _backlog_bury_after_fail_limit()
        )
        self.backlog_bury_after_fail_limit.setEnabled(
            self.backlog_bury_after_fail.isChecked()
        )
        self.backlog_bury_after_fail.toggled.connect(
            self.backlog_bury_after_fail_limit.setEnabled
        )
        bury_after_fail_layout.addWidget(self.backlog_bury_after_fail)
        bury_after_fail_layout.addWidget(self.backlog_bury_after_fail_limit)
        bury_after_fail_layout.addWidget(QLabel("fails"))
        bury_after_fail_layout.addStretch(1)
        backlog_layout.addWidget(bury_after_fail_row)

        self.backlog_suppress_new_cards = QCheckBox("Temporarily suppress new cards")
        self.backlog_suppress_new_cards.setToolTip(
            "Runtime-only: hide new cards while studying and hide the New "
            "column in the deck browser. This does not change deck presets."
        )
        self.backlog_suppress_new_cards.setChecked(
            bool(cfg["backlog_suppress_new_cards"])
        )
        backlog_layout.addWidget(self.backlog_suppress_new_cards)

        today_only_button = QPushButton("Today Only - Set all New to 0")
        today_only_button.setToolTip(
            "Set each normal deck's Today Only new-card limit to 0 for "
            "Anki's current day."
        )
        today_only_button.clicked.connect(
            lambda _checked=False: _confirm_today_only_new_zero(self)
        )
        backlog_layout.addWidget(today_only_button)
        backlog_layout.addStretch(1)

        options_layout.addWidget(addon_options, 1)
        options_layout.addWidget(_vertical_divider())
        options_layout.addWidget(backlog_options, 1)
        layout.addWidget(options_row)

        layout.addWidget(_section_divider())

        decks = sorted(
            mw.col.decks.all_names_and_ids(
                skip_empty_default=False, include_filtered=False
            ),
            key=lambda d: d.name.lower(),
        )
        self.spin_groups: dict[str, list[QSpinBox]] = {}

        configured = []
        all_rows = []
        for deck in decks:
            target = self._target_value(targets, deck.id)
            all_rows.append((deck, target))
            if target > 0:
                configured.append((deck, target))

        layout.addWidget(QLabel("<b>Currently Configured Deck Times</b>"))
        self.configured_table = self._make_configured_table(configured)
        layout.addWidget(self.configured_table, 1)

        layout.addWidget(_section_divider())

        layout.addWidget(QLabel("<b>Add a New Deck Timer</b>"))
        self.available_tree = self._make_tree(all_rows)
        layout.addWidget(self.available_tree, 2)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _target_value(self, targets: dict, did: int) -> int:
        try:
            return int(targets.get(str(did), 0))
        except (TypeError, ValueError):
            return 0

    def _make_spin(self, did: int, target: int) -> QSpinBox:
        sp = NoWheelSpinBox()
        sp.setRange(0, 1440)
        sp.setStyleSheet(SPINBOX_NO_ARROWS)
        sp.setSpecialValueText("")
        sp.setValue(target)
        self._register_spin(did, sp)
        return sp

    def _register_spin(self, did: int, spin: QSpinBox) -> None:
        key = str(did)
        group = self.spin_groups.setdefault(key, [])
        group.append(spin)

        def sync(value: int, key=key, source=spin) -> None:
            for other in self.spin_groups.get(key, []):
                if other is not source and other.value() != value:
                    other.setValue(value)

        spin.valueChanged.connect(sync)

    def _make_configured_table(self, rows) -> QTableWidget:
        table = QTableWidget(len(rows), 4)
        table.setHorizontalHeaderLabels(
            ["Deck", " minutes per day", "Sort option", ""]
        )
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        for row, (deck, target) in enumerate(rows):
            item = QTableWidgetItem(deck.name)
            item.setToolTip(deck.name)
            item.setData(Qt.ItemDataRole.UserRole, str(deck.id))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 0, item)
            table.setCellWidget(row, 1, self._make_spin(deck.id, target))
            table.setCellWidget(row, 2, self._make_sort_option_widget(deck.id))
            table.setCellWidget(row, 3, self._make_remove_button(deck.id))

        table.setColumnWidth(0, SETTINGS_DECK_COLUMN_WIDTH)
        return table

    def _make_sort_option_widget(self, did: int) -> QWidget:
        label_text, is_ok = _deck_sort_option_status(did)
        color = "#2e7d32" if is_ok else "#c62828"
        background = "transparent" if is_ok else "#ffebee"
        border = "transparent" if is_ok else "#ef9a9a"

        cell = QWidget()
        cell.setObjectName("sortOptionCell")
        cell.setToolTip(label_text if is_ok else SORT_OPTION_WARNING)
        cell.setStyleSheet(
            "QWidget#sortOptionCell {"
            "background: %s;"
            "border: 1px solid %s;"
            "border-radius: 3px;"
            "}" % (background, border)
        )
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(4)

        label = QLabel(label_text)
        label.setStyleSheet("color: %s;" % color)
        label.setToolTip(label_text if is_ok else SORT_OPTION_WARNING)
        layout.addWidget(label)

        if not is_ok:
            info = QLabel("(i)")
            info.setStyleSheet("color: %s; font-weight: bold;" % color)
            info.setToolTip(SORT_OPTION_WARNING)
            layout.addWidget(info)

        return cell

    def _make_remove_button(self, did: int) -> QToolButton:
        btn = QToolButton()
        btn.setToolTip("Remove timer")
        btn.setAutoRaise(True)
        btn.setText("x")
        btn.setStyleSheet(
            "QToolButton { color: #888888; border: none; font-size: 13px; }"
            "QToolButton:hover { color: #d32f2f; }"
        )
        btn.clicked.connect(lambda _checked=False, did=did: self._remove_timer(did))
        return btn

    def _remove_timer(self, did: int) -> None:
        key = str(did)
        for spin in list(self.spin_groups.get(key, [])):
            spin.setValue(0)

        for row in range(self.configured_table.rowCount()):
            item = self.configured_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == key:
                spin = self.configured_table.cellWidget(row, 1)
                if spin in self.spin_groups.get(key, []):
                    self.spin_groups[key].remove(spin)
                self.configured_table.removeRow(row)
                break

    def _make_tree(self, rows) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setColumnCount(2)
        tree.setHeaderLabels(["Deck", " minutes per day"])
        tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tree.setUniformRowHeights(True)
        tree.setAnimated(True)
        tree.setAlternatingRowColors(True)

        items_by_path: dict[str, QTreeWidgetItem] = {}
        for deck, target in rows:
            parent = None
            path = ""
            parts = deck.name.split("::")
            for part in parts:
                path = part if not path else path + "::" + part
                item = items_by_path.get(path)
                if item is None:
                    item = QTreeWidgetItem([part, ""])
                    item.setToolTip(0, path)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if parent is None:
                        tree.addTopLevelItem(item)
                    else:
                        parent.addChild(item)
                    items_by_path[path] = item
                parent = item

            if parent is None:
                continue
            parent.setToolTip(0, deck.name)

            tree.setItemWidget(parent, 1, self._make_spin(deck.id, target))

        tree.collapseAll()
        tree.setColumnWidth(0, SETTINGS_DECK_COLUMN_WIDTH)
        return tree

    def _save(self) -> None:
        global _blocked
        cfg = _config()
        cfg.pop("low_threshold", None)
        cfg.pop("high_threshold", None)
        cfg.pop("extend_batch", None)
        cfg.pop("over_limit_grace_minutes", None)
        cfg["postpone_fails_after_target"] = self.postpone_fails.isChecked()
        cfg["postpone_all_after_grace"] = self.postpone_all.isChecked()
        cfg["recommended_order"] = self.recommended_order.isChecked()
        cfg["over_limit_grace_percent"] = int(
            self.over_limit_grace_percent.value()
        )
        cfg["backlog_bury_after_fail"] = self.backlog_bury_after_fail.isChecked()
        cfg["backlog_bury_after_fail_limit"] = int(
            self.backlog_bury_after_fail_limit.value()
        )
        cfg["backlog_suppress_new_cards"] = (
            self.backlog_suppress_new_cards.isChecked()
        )
        cfg["suppress_new_cards"] = cfg["backlog_suppress_new_cards"]
        cfg["targets"] = {
            did: int(spins[0].value())
            for did, spins in self.spin_groups.items()
            if spins and int(spins[0].value()) > 0
        }
        _save_config(cfg)
        _blocked = None
        if mw.deckBrowser:
            mw.deckBrowser.refresh()
        self.accept()


def _open_settings() -> None:
    SettingsDialog(mw).exec()


# Registration ----------------------------------------------------------------

gui_hooks.webview_will_set_content.append(_on_webview_will_set_content)
gui_hooks.webview_did_receive_js_message.append(_on_js_message)
gui_hooks.deck_browser_will_render_content.append(
    _on_deck_browser_will_render_content
)
gui_hooks.deck_browser_did_render.append(_on_deck_browser_did_render)
gui_hooks.reviewer_did_answer_card.append(_on_reviewer_did_answer_card)
gui_hooks.state_did_change.append(_on_state_did_change)
_install_scheduler_patch()
