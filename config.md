# Time Per Deck

Turns selected decks into time-budgeted study sessions. Set daily targets from
the deck list with the **Minutes per deck** button. On the deck list, each
configured deck replaces its **New/Learn/Due** counts with a progress bar for
minutes studied relative to the target.
During review, the bottom status area shows two time progress bars: the current
target deck on top and the overall total for all configured target decks below.

For each deck with a target of `T` minutes, the time spent answering cards today
(including sub-decks) controls the workflow:

- **Before target** (`spent < T`): if the deck runs out of available work, the
  addon pulls more new cards beyond the deck's normal daily new-card limit.
- **At target** (`spent >= T`): remaining new cards in the deck are buried until
  tomorrow. Due reviews can still be finished.
- **Fail postponing**: when enabled, an **Again** after the target is reached
  buries that card until tomorrow.
- **Grace-limit postponing**: when enabled, remaining new, due review, and
  learning cards are buried after the configured grace percentage past the deck
  target, and the target deck's Today Only new-card limit is set to `0`.
- **Recommended Order**: when enabled, cards are presented as due reviews that
  have not failed today, review cards that have failed today, then new cards,
  preserving Anki's ordering within each group.
- **Backlog Clear**: optional controls in the same settings window can hide all
  new cards at runtime, auto-bury cards after a configurable number of **Again**
  answers today, or set every normal deck's Today Only new-card limit to `0`.
  Deck-list progress hover text shows a per-deck min/max range.

Configuration:

- `targets` - map of `deck id` to target minutes per day. Edit this from the
  dialog; decks with no entry or `0` are ignored and show a blank column.
- `postpone_fails_after_target` - bury failed cards after the target is reached.
- `postpone_all_after_grace` - bury remaining cards after the grace period.
- `over_limit_grace_percent` - percent past the target before bulk postponing
  remaining cards. Default: `20`.
- `recommended_order` - present due reviews that have not failed today first,
  review cards that have failed today second, and new cards last. Default:
  `true`.
- `backlog_suppress_new_cards` - hide new cards while studying and hide the New
  column in the deck browser without changing deck presets. Default: `false`.
- `backlog_bury_after_fail` - auto-bury a card after repeated **Again** answers
  today. Default: `true`.
- `backlog_bury_after_fail_limit` - number of **Again** answers before
  auto-burying. Default: `5`.
