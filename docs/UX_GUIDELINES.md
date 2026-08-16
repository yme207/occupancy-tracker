# UX & Visual Design Guidelines

Applies to the topology editor panel and any other user-facing surface this integration ships
(`custom_components/occupancy_tracker/www/`). The bar: **it should look and feel like it belongs
inside Home Assistant, built by someone who obsesses over detail — not like a generic AI-generated
admin panel bolted on the side.**

## 1. Don't invent a design system — borrow HA's

The single biggest lever against an "AI slop" feel is **not designing from scratch**. Home
Assistant's frontend is Lit-based, uses Material Design 3 tokens, and ships a large set of reusable
custom elements. Use them:

- Build with HA's own components/idioms (`ha-card`, `ha-icon`, `ha-textfield`, `ha-switch`,
  `ha-slider` or equivalent, `mwc-*`/`ha-*` form controls) rather than a bespoke component library
  or raw unstyled HTML.
- Theme exclusively through Home Assistant's CSS custom properties (`--primary-color`,
  `--card-background-color`, etc.) — never hardcoded hex colors. This is also what makes light/dark
  and custom HA themes work for free instead of needing separate handling.
- Match HA's spacing, type scale, and elevation conventions rather than inventing new ones. If it
  looks inconsistent sitting next to the rest of the HA "Devices & Services" UI, it's wrong.

## 2. Motion and interaction

- Transitions should be purposeful, not decorative: a connector being drawn, a room's confidence
  changing, a panel opening — these deserve a smooth transition. Static state changes (e.g. a
  label updating) don't need animation bolted on for its own sake.
  Purposeful minimal motion reads as crafted; enough animation to be genuinely showy per
  interaction reads like a template.
- Interactions should feel immediate: optimistic UI updates when the user draws a connector or
  toggles an egress point (update the view first, reconcile with the backend save quietly after),
  not a spinner for a trivial local edit.

## 3. The "witchcraft" feel

The product should feel effortlessly capable, not like a configuration tool the user has to
operate carefully. Concretely:

- **Confident, sensible defaults.** Immediately after setup — before any manual tuning — the
  integration should already behave usefully off the entities it can see. If it needs a lot of
  manual tuning to feel right, that's a product problem to fix in the engine/defaults, not
  something to paper over in the UI.
- **Visible intelligence, on demand, not by default.** The topology editor should let a user click
  a room and see *why* it currently thinks what it thinks (active signals, confidence tier,
  transit chain) — the reasoning should be one interaction away, not hidden in logs and not forced
  on the user constantly.
- **Proactive, not just reactive, where it's earned.** Once the system has enough signal history,
  surfacing an observation the user didn't ask for (e.g. "this connector has never shown activity
  — is it actually walkable, or should egress here be modeled differently?") reads as intelligent.
  This is a stretch feature, not a v1 requirement — don't force it in before the underlying signal
  history actually supports a real observation.
- **Setup should feel like magic, not paperwork.** Since rooms/devices are pulled from HA directly
  (`SPEC.md` §5), the first thing a user sees after adding the integration should already be
  populated and mostly correct, with the topology editor as the one deliberate, satisfying step
  they take themselves — not a wall of empty fields to fill in.

## 4. Tunable parameters

Every tunable exposed to the user (decay/confirmation windows, confidence thresholds, household
size hint, near-house zones — see `SPEC.md` §7.2/§7.3) must be presented as:

- A real control (slider, stepper, toggle) with sensible min/max/step and a live-updating value
  label — never a bare numeric text field with no context.
- A short, plain-language description of *what changing it actually does* ("higher = occupancy
  persists longer after signals go quiet"), not just its internal name.
- A sensible default that works without being touched — tunables are for refinement, not
  mandatory setup.

## 5. Content quality

- No placeholder/lorem-ipsum copy, no generic icon substitutes, no "TODO" text shipped to users.
- Error and empty states are written deliberately (what happened, what to do next) — not raw
  exception text or a blank panel.
- Copy is concise and matches Home Assistant's own tone (direct, plain, approachable).

### 5.1 Plain language, always (durable standard, not a one-off)

**Every piece of text an end user actually reads — not code comments, not `docs/*.md` — must
assume no technical or algorithmic background.** This applies everywhere a person using Home
Assistant (not a developer reading this repo) encounters text from this integration:
`translations/en.json` (config flow, options flow, service names/descriptions), the topology
panel's copy (`custom_components/occupancy_tracker/www/`), and user-facing prose in `README.md`
(installation/usage sections a HACS user reads, as opposed to developer-oriented sections).

Concretely:

- Describe cause and effect ("turn this up if people in your home often take a while getting
  between rooms"), not internal mechanics ("increases the transit-confirmation window").
- No jargon from this project's own internals — "transit," "provenance," "quality tier,"
  "connector," "topology graph," "occupant token," etc. are working vocabulary for developers
  reading `docs/SPEC.md`/`docs/ARCHITECTURE.md`, never words to show a user directly. Prefer
  "room," "sensor," "how sure it is," "the layout you've drawn."
- Prefer a short, concrete example over an abstract description when one clarifies faster.
- This standard originated from direct project-owner feedback (`docs/DECISIONS.md`'s 2026-08-09
  "Options-flow/service translations rewritten for non-technical users" entry: "the language needs
  to assume the user doesn't have an understanding of the code or technical methods... dumb it
  down to simple concepts they can understand") — treat it as durable, not specific to that one
  rewrite. Apply it to *every* new or edited piece of user-facing text going forward, not just the
  surfaces it was first applied to.

## 6. Accessibility baseline

- Sufficient color contrast in both light and dark themes (don't rely on color alone to convey
  state — pair with icon/label).
- Keyboard-operable topology editor where feasible (at minimum, don't make graph editing the
  *only* way to configure something functionally essential — an accessible fallback path matters,
  even if the graphical editor is the primary experience).

## 7. Review checklist before calling a UI slice done

- [ ] Uses HA native components/theming, not custom-styled-from-scratch equivalents.
- [ ] Works in both light and dark theme.
- [ ] No hardcoded colors/spacing outside HA's design tokens.
- [ ] Every tunable has a label, sensible range, and a plain-language description.
- [ ] All new/edited user-facing text meets §5.1's plain-language standard (no project jargon,
      cause-and-effect phrasing, no assumed technical background).
- [ ] No placeholder content remains.
- [ ] Interaction feels immediate (optimistic updates where appropriate).
