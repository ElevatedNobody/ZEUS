# ZEUS design system - "Voice Core"

The owner's design (supplied 2026-09-27 as a single HTML mock): a pitch-black void, an
iridescent voice wave across the middle of the screen, frosted-glass panels floating over
it, a top bar with MIC / AUDIO / CINEMATIC VIEW, prompt chips, and a command line. ZEUS
implements that look offline and honestly - every panel is fed by the backend; nothing in
it is decorative data. Files: `ui/medha/css/tokens.css` (the only place colours, type,
radii, depth and motion are defined), `base.css`, `components.css`, `auth.css`,
`screens.css`; the wave is `ui/medha/js/voicewave.js`.

## What was taken from the mock - and what was not

| In the mock | In ZEUS |
|---|---|
| Tailwind from a CDN, Google Fonts | hand-written CSS; fonts by local name only (see Type) - the window loads nothing from a network |
| fixed numbers (82,450 BPD, 4,120 PSI, a 43/22/18/9 % product split, "+4.8%") | the side cards draw `plant_telemetry`: crude throughput and its change vs the previous month, utilisation, downtime, on-spec, the rising sensor series, equipment by status, the top open alert; a missing value is "—"; DEMO data is labelled DEMO |
| "I'm listening" as a permanent headline | the line says what the backend state is (IDLE "Ready when you are", LISTENING "I'm listening", THINKING, SPEAKING...) |
| a microphone opened by the page (getUserMedia) and a 52 Hz drone | the page has no microphone: MIC runs the local Python recogniser once; the wave follows the level it reports; AUDIO switches ZEUS's spoken replies |
| "ACOUSTIC STREAM NOMINAL", "MIC IN: 48 kHz", "SYNCHRONIZED" | not shown - nothing measured them |
| ZEUS / PetroVision branding | ZEUS (the product was renamed; nothing is called ZEUS - `tests/test_chatterbox_voice.py`) |
| an endless `requestAnimationFrame` loop, per-frame `shadowBlur` | see Motion - the wave moves only while something happens |

`tests/test_medha_interface_behaviour.py::test_the_design_mock_numbers_never_reach_the_interface`
fails if any of the mock's figures or names appear in the interface.

## Colour

| Token | Value | Use |
|---|---|---|
| `--crude` | `#000000` | the void behind everything |
| `--glass` | 135° `rgba(12,18,28,.72)` → `rgba(3,7,14,.55)` + 22 px blur | cards, rail, command banner, sign-in card |
| `--glass-line` | `rgba(255,255,255,.08)` | hairlines; cyan `rgba(0,242,254,.35)` on hover |
| `--steel-1/2/3` | `#0B111A` / `#131B27` / `#223044` | solid surfaces (table headers, tracks) |
| `--ink` / `--ink-2` / `--ink-3` | `#F1F5F9` / `#AAB6C7` / `#7D8BA0` | text: primary, secondary, muted |
| `--cyan` | `#00F2FE` | the accent: focus, live state, primary actions, card titles |
| `--sky` | `#38BDF8` | information, charts |
| `--violet` | `#A855F7` | SAMPLE / DEMO labels, gradient middle |
| `--magenta` | `#F43F5E` | failure, gradient end |
| `--gold` / `--emerald` | `#F59E0B` / `#10B981` | warning / ok (text uses the lighter `--warn` `#FBBF24`, `--ok` `#34D399`) |
| `--gradient-flare` | cyan → violet → magenta | progress, utilisation, story segments |
| `--gradient-ring` | cyan-500 → violet → pink | the ZEUS badge ring, avatar |

Status colours are used **only** for a state the backend reported.

## Type

Local fonts only: `Plus Jakarta Sans` and `JetBrains Mono` are used **when installed on the
machine** (Windows → Fonts); otherwise Segoe UI Variable and Cascadia Mono / Consolas.
Nothing is downloaded. Labels, card titles, chips and numbers are mono, uppercase,
tracked (the mock's `font-mono tracking-widest`). Scale 11 / 12 / 13 / 15 / 18 / 24 / 32 /
44; nothing is drawn below 10 px (`test_no_text_is_drawn_below_ten_pixels`).

## Shape and depth

Radii 8 / 12 / 16 / 24 (chips 6, buttons 12, cards 16, sheets and screens 24). Glass
cards carry the mock's deep drop shadow and a 1 px top highlight; on panel screens the
cards inside a glass panel drop their own blur (one blur per surface, not nested).

## Motion

| Token | Value |
|---|---|
| `--ease-spring` | `cubic-bezier(.16,1,.3,1)` (the mock's curve) |
| `--t-fast` / `--t-med` / `--t-slow` | 180 / 320 / 500 ms |

**The wave** (`voicewave.js`): five additive ribbons with a bell envelope, as in the mock.
It moves while ZEUS is LISTENING / UNDERSTANDING / THINKING / RETRIEVING / EXECUTING /
SPEAKING, for 1.8 s after a command is sent, for a moment when the workspace opens, and
on the sign-in screen while somebody is there (12 s after the last key or pointer move).
Otherwise it draws one still frame and stops. It advances by elapsed time, runs one loop
at most, uses no per-frame shadow blur, builds gradients once per resize, and stops when
the window is hidden or reduced motion is asked for. Measured idle cost: 0 animation
frames in 3 s (`tests/e2e/test_medha_e2e.py::test_idle_costs_no_animation_frames`).

CSS: transform and opacity only; an infinite animation is allowed only on a state that
ends (spinner, loading bar, live scan, active pipeline step, `.live` text)
(`test_nothing_animates_forever_unless_something_is_happening`).

## Layout

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ [◉] ZEUS  VOICE CORE v3   status chips   clock/state  MIC  AUDIO  CINEMATIC  user │
 ├────┬─────────────────────────────────────────────────────────────────────┤
 │rail│          "Ready when you are" / "I'm listening" (backend state)      │
 │    │  ┌ FIELD TELEMETRY ┐        ～～～ voice wave ～～～       ┌ OPERATIONS ┐│
 │    │  └─────────────────┘        conversation (glass)          └────────────┘│
 │    │   • Plant status  • Open alerts  • Trend C-301  ...  History  Last run  │
 ├────┴─────────────────────────────────────────────────────────────────────┤
 │                  ● ZEUS:  command line ....................  READY  Send  │
 └──────────────────────────────────────────────────────────────────────────┘
```

Other screens open as one glass panel over the dimmed, still wave. **Cinematic view**
(button, or Esc when no dialog, menu, sheet or field has focus) hides every panel and
leaves the wave and the voice line; Esc or Ctrl+K brings them back.

## Signature components

* **Voice line** - the backend state in words, over the wave.
* **Side cards** - glass telemetry panels with a gradient sparkline, a spectrum bar and
  mono tiles, drawn from `plant_telemetry`.
* **Prompt chips** - every quick action in one scrolling row (dot colours cycle cyan /
  violet / magenta).
* **Story rail** - the live pipeline as segmented bars, each filling only when the backend
  emits that stage.
* **Answer tables** - register answers render as tables whose cells keep their citation
  chips.
* **Scanner ring** - face sign-in: preview in a ring whose segments show captured frames.

## Honesty rules (enforced by tests)

* Every number, state and badge renders a value the backend returned; unknown is "—" or
  UNAVAILABLE with the reason.
* MIC and AUDIO say UNAVAILABLE (and are disabled) when the local voice stack cannot do it;
  GPU is named only when the loaded provider reported CUDA.
* "SECURE", "VERIFIED", "PROTECTED" appear only as the name of a backend state.
